"""SEG-Y geometry handling functions."""


from __future__ import annotations

import logging
import time
from abc import ABC
from abc import abstractmethod
from enum import Enum
from enum import auto
from typing import Sequence

import numpy as np
import numpy.typing as npt

from mdio.segy.exceptions import GridOverrideIncompatibleError
from mdio.segy.exceptions import GridOverrideKeysError
from mdio.segy.exceptions import GridOverrideMissingParameterError
from mdio.segy.exceptions import GridOverrideUnknownError


logger = logging.getLogger(__name__)


class StreamerShotGeometryType(Enum):
    r"""Shot  geometry template types for streamer acquisition.

    Configuration A:
        Cable 1 ->          1------------------20
        Cable 2 ->         1-----------------20
        .                 1-----------------20
        .          ⛴ ☆  1-----------------20
        .                 1-----------------20
        Cable 6 ->         1-----------------20
        Cable 7 ->          1-----------------20


    Configuration B:
        Cable 1 ->          1------------------20
        Cable 2 ->         21-----------------40
        .                 41-----------------60
        .          ⛴ ☆  61-----------------80
        .                 81----------------100
        Cable 6 ->         101---------------120
        Cable 7 ->          121---------------140

    Configuration C:
        Cable ? ->        / 1------------------20
        Cable ? ->       / 21-----------------40
        .               / 41-----------------60
        .          ⛴ ☆ - 61-----------------80
        .               \ 81----------------100
        Cable ? ->       \ 101---------------120
        Cable ? ->        \ 121---------------140
    """

    A = auto()
    B = auto()
    C = auto()


def analyze_streamer_headers(
    index_headers: dict[str, npt.NDArray],
) -> tuple[npt.NDArray, npt.NDArray, npt.NDArray, StreamerShotGeometryType]:
    """Check input headers for SEG-Y input to help determine geometry.

    This function reads in trace_qc_count headers and finds the unique cable values.
    The function then checks to make sure channel numbers for different cables do
    not overlap.

    Args:
        index_headers: numpy array with index headers

    Returns:
        tuple of unique_cables, cable_chan_min, cable_chan_max, geom_type
    """
    # Find unique cable ids
    unique_cables = np.sort(np.unique(index_headers["cable"]))

    # Find channel min and max values for each cable
    cable_chan_min = np.empty(unique_cables.shape)
    cable_chan_max = np.empty(unique_cables.shape)

    for idx, cable in enumerate(unique_cables):
        cable_mask = index_headers["cable"] == cable
        current_cable = index_headers["channel"][cable_mask]

        cable_chan_min[idx] = np.min(current_cable)
        cable_chan_max[idx] = np.max(current_cable)

    # Check channel numbers do not overlap for case B
    geom_type = StreamerShotGeometryType.B
    for idx, cable in enumerate(unique_cables):
        min_val = cable_chan_min[idx]
        max_val = cable_chan_max[idx]
        for idx2, cable2 in enumerate(unique_cables):
            if cable2 == cable:
                continue

            if cable_chan_min[idx2] < max_val and cable_chan_max[idx2] > min_val:
                geom_type = StreamerShotGeometryType.A

    return unique_cables, cable_chan_min, cable_chan_max, geom_type


def create_counter(
    depth: int,
    total_depth: int,
    unique_headers: dict[str, npt.NDArray],
    header_names: list[str],
):
    """Helper funtion to create dictionary tree for counting trace key for auto index."""
    if depth == total_depth:
        return 0

    counter = {}

    header_key = header_names[depth]
    for header in unique_headers[header_key]:
        counter[header] = create_counter(
            depth + 1, total_depth, unique_headers, header_names
        )

    return counter


def create_trace_index(
    depth: int,
    counter: dict,
    index_headers: dict[str, npt.NDArray],
    header_names: list,
    dtype=np.int16,
):
    """Update dictionary counter tree for counting trace key for auto index."""
    # Add index header
    index_headers["trace"] = np.empty(index_headers[header_names[0]].shape, dtype=dtype)

    idx = 0
    if depth == 0:
        return

    for idx_values in zip(  # noqa: B905
        *(index_headers[header_names[i]] for i in range(depth))
    ):
        if depth == 1:
            counter[idx_values[0]] += 1
            index_headers["trace"][idx] = counter[idx_values[0]]
        else:
            sub_counter = counter
            for idx_value in idx_values[:-1]:
                sub_counter = sub_counter[idx_value]
            sub_counter[idx_values[-1]] += 1
            index_headers["trace"][idx] = sub_counter[idx_values[-1]]

        idx += 1


def analyze_non_indexed_headers(
    index_headers: dict[str, npt.NDArray], dtype=np.int16
) -> dict[str, npt.NDArray]:
    """Check input headers for SEG-Y input to help determine geometry.

    This function reads in trace_qc_count headers and finds the unique cable values.
    The function then checks to make sure channel numbers for different cables do
    not overlap.

    Args:
        index_headers: numpy array with index headers
        dtype: numpy type for value of created trace header.

    Returns:
        Dict container header name as key and numpy array of values as value
    """
    # Find unique cable ids
    t_start = time.perf_counter()
    unique_headers = {}
    total_depth = 0
    header_names = []
    for header_key in index_headers.keys():
        if header_key != "trace":
            unique_headers[header_key] = np.sort(np.unique(index_headers[header_key]))
            header_names.append(header_key)
            total_depth += 1

    counter = create_counter(0, total_depth, unique_headers, header_names)

    create_trace_index(total_depth, counter, index_headers, header_names, dtype=dtype)

    t_stop = time.perf_counter()
    logger.debug(f"Time spent generating trace index: {t_start - t_stop:.4f} s")
    return index_headers


class GridOverrideCommand(ABC):
    """Abstract base class for grid override commands."""

    @property
    @abstractmethod
    def required_keys(self) -> set:
        """Get the set of required keys for the grid override command."""

    @property
    @abstractmethod
    def required_parameters(self) -> set:
        """Get the set of required parameters for the grid override command."""

    @abstractmethod
    def validate(
        self, index_headers: npt.NDArray, grid_overrides: dict[str, bool | int]
    ) -> None:
        """Validate if this transform should run on the type of data."""

    @abstractmethod
    def transform(
        self,
        index_headers: dict[str, npt.NDArray],
        grid_overrides: dict[str, bool | int],
    ) -> dict[str, npt.NDArray]:
        """Perform the grid transform."""

    def transform_index_names(
        self,
        index_names: Sequence[str],
    ) -> Sequence[str]:
        """Perform the transform of index names.

        Optional method: Subclasses may override this method to provide
        custom behavior. If not overridden, this default implementation
        will be used, which is a no-op.

        Args:
            index_names: List of index names to be modified.

        Returns:
            New tuple of index names after the transform.
        """
        return index_names

    def transform_chunksize(
        self,
        chunksize: Sequence[int],
        grid_overrides: dict[str, bool | int],
    ) -> Sequence[int]:
        """Perform the transform of chunksize.

        Optional method: Subclasses may override this method to provide
        custom behavior. If not overridden, this default implementation
        will be used, which is a no-op.

        Args:
            chunksize: List of chunk sizes to be modified.
            grid_overrides: Full grid override parameterization.

        Returns:
            New tuple of chunk sizes after the transform.
        """
        return chunksize

    @property
    def name(self) -> str:
        """Convenience property to get the name of the command."""
        return self.__class__.__name__

    def check_required_keys(self, index_headers: dict[str, npt.NDArray]) -> None:
        """Check if all required keys are present in the index headers."""
        index_names = index_headers.keys()
        if not self.required_keys.issubset(index_names):
            raise GridOverrideKeysError(self.name, self.required_keys)

    def check_required_params(self, grid_overrides: dict[str, str | int]) -> None:
        """Check if all required keys are present in the index headers."""
        if self.required_parameters is None:
            return

        passed_parameters = set(grid_overrides.keys())

        if not self.required_parameters.issubset(passed_parameters):
            missing_params = self.required_parameters - passed_parameters
            raise GridOverrideMissingParameterError(self.name, missing_params)


class DuplicateIndex(GridOverrideCommand):
    """Automatically handle duplicate traces in a new axis - trace with chunksize 1."""

    required_keys = None
    required_parameters = None

    def validate(
        self,
        index_headers: dict[str, npt.NDArray],
        grid_overrides: dict[str, bool | int],
    ) -> None:
        """Validate if this transform should run on the type of data."""
        if "ChannelWrap" in grid_overrides:
            raise GridOverrideIncompatibleError(self.name, "ChannelWrap")

        if "CalculateCable" in grid_overrides:
            raise GridOverrideIncompatibleError(self.name, "CalculateCable")

        if self.required_keys is not None:
            self.check_required_keys(index_headers)
        self.check_required_params(grid_overrides)

    def transform(
        self,
        index_headers: dict[str, npt.NDArray],
        grid_overrides: dict[str, bool | int],
    ) -> dict[str, npt.NDArray]:
        """Perform the grid transform."""
        self.validate(index_headers, grid_overrides)

        return analyze_non_indexed_headers(index_headers)

    def transform_index_names(
        self,
        index_names: Sequence[str],
    ) -> Sequence[str]:
        """Insert dimension "trace" to the sample-1 dimension."""
        new_names = list(index_names)
        new_names.append("trace")
        return tuple(new_names)

    def transform_chunksize(
        self,
        chunksize: Sequence[int],
        grid_overrides: dict[str, bool | int],
    ) -> Sequence[int]:
        """Insert chunksize of 1 to the sample-1 dimension."""
        new_chunks = list(chunksize)
        new_chunks.insert(-1, 1)
        return tuple(new_chunks)


class NonBinned(DuplicateIndex):
    """Automatically index traces in a single specified axis - trace."""

    required_keys = None
    required_parameters = {"chunksize"}

    def transform_chunksize(
        self,
        chunksize: Sequence[int],
        grid_overrides: dict[str, bool | int],
    ) -> Sequence[int]:
        """Perform the transform of chunksize."""
        new_chunks = list(chunksize)
        new_chunks.insert(-1, grid_overrides["chunksize"])
        return tuple(new_chunks)


class AutoChannelWrap(GridOverrideCommand):
    """Automatically determine Streamer acquisition type."""

    required_keys = {"shot_point", "cable", "channel"}
    required_parameters = None

    def validate(
        self,
        index_headers: dict[str, npt.NDArray],
        grid_overrides: dict[str, bool | int],
    ) -> None:
        """Validate if this transform should run on the type of data."""
        if "ChannelWrap" in grid_overrides:
            raise GridOverrideIncompatibleError(self.name, "ChannelWrap")

        if "CalculateCable" in grid_overrides:
            raise GridOverrideIncompatibleError(self.name, "CalculateCable")

        self.check_required_keys(index_headers)
        self.check_required_params(grid_overrides)

    def transform(
        self,
        index_headers: dict[str, npt.NDArray],
        grid_overrides: dict[str, bool | int],
    ) -> dict[str, npt.NDArray]:
        """Perform the grid transform."""
        self.validate(index_headers, grid_overrides)

        result = analyze_streamer_headers(index_headers)
        unique_cables, cable_chan_min, cable_chan_max, geom_type = result
        logger.info(f"Ingesting dataset as {geom_type.name}")

        # TODO: Add strict=True and remove noqa when min Python is 3.10
        for cable, chan_min, chan_max in zip(  # noqa: B905
            unique_cables, cable_chan_min, cable_chan_max
        ):
            logger.info(
                f"Cable: {cable} has min chan: {chan_min} and max chan: {chan_max}"
            )

        # This might be slow and potentially could be improved with a rewrite
        # to prevent so many lookups
        if geom_type == StreamerShotGeometryType.B:
            for idx, cable in enumerate(unique_cables):
                cable_idxs = np.where(index_headers["cable"][:] == cable)
                cc_min = cable_chan_min[idx]

                index_headers["channel"][cable_idxs] = (
                    index_headers["channel"][cable_idxs] - cc_min + 1
                )

        return index_headers


class ChannelWrap(GridOverrideCommand):
    """Wrap channels to start from one at cable boundaries."""

    required_keys = {"shot_point", "cable", "channel"}
    required_parameters = {"ChannelsPerCable"}

    def validate(
        self, index_headers: dict, grid_overrides: dict[str, bool | int]
    ) -> None:
        """Validate if this transform should run on the type of data."""
        if "AutoChannelWrap" in grid_overrides:
            raise GridOverrideIncompatibleError(self.name, "AutoCableChannel")

        self.check_required_keys(index_headers)
        self.check_required_params(grid_overrides)

    def transform(
        self,
        index_headers: dict[str, npt.NDArray],
        grid_overrides: dict[str, bool | int],
    ) -> dict[str, npt.NDArray]:
        """Perform the grid transform."""
        self.validate(index_headers, grid_overrides)

        channels_per_cable = grid_overrides["ChannelsPerCable"]
        index_headers["channel"] = (
            index_headers["channel"] - 1
        ) % channels_per_cable + 1

        return index_headers


class CalculateCable(GridOverrideCommand):
    """Calculate cable numbers from unwrapped channels."""

    required_keys = {"shot_point", "cable", "channel"}
    required_parameters = {"ChannelsPerCable"}

    def validate(
        self,
        index_headers: dict[str, npt.NDArray],
        grid_overrides: dict[str, bool | int],
    ) -> None:
        """Validate if this transform should run on the type of data."""
        if "AutoChannelWrap" in grid_overrides:
            raise GridOverrideIncompatibleError(self.name, "AutoCableChannel")

        self.check_required_keys(index_headers)
        self.check_required_params(grid_overrides)

    def transform(
        self,
        index_headers: dict[str, npt.NDArray],
        grid_overrides: dict[str, bool | int],
    ) -> dict[str, npt.NDArray]:
        """Perform the grid transform."""
        self.validate(index_headers, grid_overrides)

        channels_per_cable = grid_overrides["ChannelsPerCable"]
        index_headers["cable"] = (
            index_headers["channel"] - 1
        ) // channels_per_cable + 1

        return index_headers


class GridOverrider:
    """Executor for grid overrides.

    We support a certain type of grid overrides, and they have to be
    implemented following the ABC's in this module.

    This class applies the grid overrides if needed.
    """

    def __init__(self):
        """Define allowed overrides and parameters here."""
        self.commands = {
            "AutoChannelWrap": AutoChannelWrap(),
            "CalculateCable": CalculateCable(),
            "ChannelWrap": ChannelWrap(),
            "NonBinned": NonBinned(),
            "HasDuplicates": DuplicateIndex(),
        }

        self.parameters = self.get_allowed_parameters()

    def get_allowed_parameters(self) -> set:
        """Get list of allowed parameters from the allowed commands."""
        parameters = set()
        for command in self.commands.values():
            if command.required_parameters is None:
                continue

            parameters.update(command.required_parameters)

        return parameters

    def run(
        self,
        index_headers: dict[str, npt.NDArray],
        index_names: Sequence[str],
        grid_overrides: dict[str, bool],
        chunksize: Sequence[int] | None = None,
    ) -> tuple[dict[str, npt.NDArray], tuple[str], tuple[int]]:
        """Run grid overrides and return result."""
        for override in grid_overrides:
            if override in self.parameters:
                continue

            if override not in self.commands:
                raise GridOverrideUnknownError(override)

            function = self.commands[override].transform
            index_headers = function(index_headers, grid_overrides=grid_overrides)

            function = self.commands[override].transform_index_names
            index_names = function(index_names)

            function = self.commands[override].transform_chunksize
            chunksize = function(chunksize, grid_overrides=grid_overrides)

        return index_headers, index_names, chunksize
