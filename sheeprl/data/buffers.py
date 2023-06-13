import typing
from typing import Optional, Sequence, Union

import torch
from tensordict import TensorDict, MemmapTensor
from tensordict.tensordict import TensorDictBase
from torch import Size, Tensor, device


class ReplayBuffer:
    def __init__(
        self,
        buffer_size: int,
        n_envs: int = 1,
        device: Union[device, str] = "cpu",
        memmap: bool = False,
    ):
        """A replay buffer which internally uses a TensorDict.

        Args:
            buffer_size (int): The buffer size.
            n_envs (int, optional): The number of environments. Defaults to 1.
            device (Union[torch.device, str], optional): The device where the buffer is created. Defaults to "cpu".
            memmap (bool, optional): Whether to memory-mapping the buffer
        """
        if buffer_size <= 0:
            raise ValueError(f"The buffer size must be greater than zero, got: {buffer_size}")
        if n_envs <= 0:
            raise ValueError(f"The number of environments must be greater than zero, got: {n_envs}")
        self._buffer_size = buffer_size
        self._n_envs = n_envs
        if isinstance(device, str):
            device = torch.device(device=device)
        self._device = device
        self._memmap = memmap
        if self._memmap:
            self._buf = None
        else:
            self._buf = TensorDict({}, batch_size=[buffer_size, n_envs], device=device)
        self._pos = 0
        self._full = False

    @property
    def buffer(self) -> Optional[TensorDictBase]:
        return self._buf

    @property
    def buffer_size(self) -> int:
        return self._buffer_size

    @property
    def full(self) -> int:
        return self._full

    @property
    def n_envs(self) -> int:
        return self._n_envs

    @property
    def shape(self) -> Size:
        return self.buffer.shape

    @property
    def device(self) -> device:
        return self._device

    def __len__(self) -> int:
        return self.buffer_size

    @typing.overload
    def add(self, data: "ReplayBuffer") -> None:
        ...

    @typing.overload
    def add(self, data: TensorDictBase) -> None:
        ...

    def add(self, data: Union["ReplayBuffer", TensorDictBase]) -> None:
        """Add data to the buffer.

        Args:
            data: data to add.

        Raises:
            RuntimeError: the number of dimensions (the batch_size of the TensorDictBase) must be 2:
            one for the number of environments and one for the sequence length.
        """
        if isinstance(data, ReplayBuffer):
            data = data.buffer
        elif not isinstance(data, TensorDictBase):
            raise TypeError("`data` must be a TensorDictBase or a sheeprl.data.ReplayBuffer")
        if len(data.shape) != 2:
            raise RuntimeError(
                "`data` must have 2 batch dimensions: [sequence_length, n_envs]. "
                "`sequence_length` and `n_envs` should be 1. Shape is: {}".format(data.shape)
            )
        data_len = data.shape[0]
        next_pos = (self._pos + data_len) % self._buffer_size
        if next_pos < self._pos or (data_len >= self._buffer_size and not self._full):
            idxes = torch.tensor(
                list(range(self._pos, self._buffer_size)) + list(range(0, next_pos)), device=self.device
            )
        else:
            idxes = torch.tensor(range(self._pos, next_pos), device=self.device)
        if data_len > self._buffer_size:
            data_to_store = data[-self._buffer_size - next_pos :]
        else:
            data_to_store = data
        if self._memmap and self._buf is None:
            self._buf = TensorDict(
                {
                    k: MemmapTensor((self._buffer_size, self._n_envs, *v.shape[2:]), dtype=v.dtype, device=v.device)
                    for k, v in data_to_store.items()
                },
                batch_size=[self._buffer_size, self._n_envs],
                device=self.device,
            )
            self._buf.memmap_()
        self._buf[idxes, :] = data_to_store
        if self._pos + data_len >= self._buffer_size:
            self._full = True
        self._pos = next_pos

    def sample(self, batch_size: int, sample_next_obs: bool = False, clone: bool = False) -> TensorDictBase:
        """Sample elements from the replay buffer.

        Custom sampling when using memory efficient variant,
        as we should not sample the element with index `self.pos`
        See https://github.com/DLR-RM/stable-baselines3/pull/28#issuecomment-637559274

        Args:
            batch_size (int): batch_size (int): Number of element to sample
            sample_next_obs (bool): whether to sample the next observations from the 'observations' key.
                Defaults to False.
            clone (bool): whether to clone the sampled TensorDict

        Returns:
            TensorDictBase: the sampled TensorDictBase with a `batch_size` of [batch_size, 1]
        """
        if batch_size <= 0:
            raise ValueError("Batch size must be greater than 0")
        if not self._full and self._pos == 0:
            raise ValueError(
                "No sample has been added to the buffer. Please add at least one sample calling `self.add()`"
            )
        if self._full:
            first_range_end = self._pos - 1 if sample_next_obs else self._pos
            second_range_end = self.buffer_size if first_range_end >= 0 else self.buffer_size + first_range_end
            valid_idxes = torch.tensor(
                list(range(0, first_range_end)) + list(range(self._pos, second_range_end)),
                device=self.device,
            )
            batch_idxes = valid_idxes[torch.randint(0, len(valid_idxes), size=(batch_size,), device=self.device)]
        else:
            max_pos_to_sample = self._pos - 1 if sample_next_obs else self._pos
            if max_pos_to_sample == 0:
                raise RuntimeError(
                    "You want to sample the next observations, but one sample has been added to the buffer. "
                    "Make sure that at least two samples are added."
                )
            batch_idxes = torch.randint(0, max_pos_to_sample, size=(batch_size,), device=self.device)
        sample = self._get_samples(batch_idxes, sample_next_obs=sample_next_obs).unsqueeze(-1)
        if clone:
            return sample.clone()
        return sample

    def _get_samples(self, batch_idxes: Tensor, sample_next_obs: bool = False) -> TensorDictBase:
        env_idxes = torch.randint(0, self.n_envs, size=(len(batch_idxes),))
        if self._buf is None:
            raise RuntimeError("The buffer has not been initialized. Try to add some data first.")
        buf = self._buf[batch_idxes, env_idxes]
        if sample_next_obs:
            buf["next_observations"] = self._buf["observations"][(batch_idxes + 1) % self._buffer_size, env_idxes]
        return buf

    def __getitem__(self, key: str) -> torch.Tensor:
        if not isinstance(key, str):
            raise TypeError("`key` must be a string")
        return self._buf.get(key)

    def __setitem__(self, key: str, t: Tensor) -> None:
        self.buffer.set(key, t, inplace=True)


class SequentialReplayBuffer(ReplayBuffer):
    """A replay buffer which internally uses a TensorDict and returns sequential samples.

    Args:
        buffer_size (int): The buffer size.
        n_envs (int, optional): The number of environments. Defaults to 1.
        device (Union[torch.device, str], optional): The device where the buffer is created. Defaults to "cpu".
    """

    def __init__(self, buffer_size: int, n_envs: int = 1, device: Union[device, str] = "cpu"):
        super().__init__(buffer_size, n_envs, device)

    def sample(
        self,
        batch_size: int,
        sample_next_obs: bool = False,
        clone: bool = False,
        sequence_length: int = 1,
        n_samples: int = 1,
    ) -> TensorDictBase:
        """Sample elements from the sequential replay buffer,
        each one is a sequence of a consecutive items.

        Custom sampling when using memory efficient variant,
        as the first element of the sequence cannot be in a position
        greater than (pos - sequence_length) % buffer_size.
        See comments in the code for more information.

        Args:
            batch_size (int): batch_size (int): Number of element to sample
            sample_next_obs (bool): whether to sample the next observations from the 'observations' key.
                Defaults to False.
            clone (bool): whether to clone the sampled TensorDict.
            sequence_length (int): the length of the sequence of each element. Defaults to 1.
            n_samples (int): the number of samples to perform. Defaults to 1.

        Returns:
            TensorDictBase: the sampled TensorDictBase with a `batch_size` of [n_samples, sequence_length, batch_size]
        """
        # the batch_size can be fused with the number of samples to have single batch size
        batch_dim = batch_size * n_samples

        # Controls
        if batch_dim <= 0:
            raise ValueError("Batch size must be greater than 0")
        if not self._full and self._pos == 0:
            raise ValueError(
                "No sample has been added to the buffer. Please add at least one sample calling `self.add()`"
            )
        if batch_dim > self._buf.shape[0]:
            raise ValueError(
                f"n_samples * batch size ({batch_dim}) is larger than the replay buffer size ({self._buf.shape[0]})"
            )
        if not self._full and self._pos - sequence_length + 1 < 1:
            raise ValueError(f"too long sequence length ({sequence_length})")
        if self.full and sequence_length > self._buf.shape[0]:
            raise ValueError(f"too long sequence length ({sequence_length})")

        # Do not sample the element with index `self.pos` as the transitions is invalid
        if self._full:
            # when the buffer is full, it is necessary to avoid the starting index between (self.pos - sequence_length)
            # and self.pos, so it is possible to sample the starting index between (0, self.pos - sequence_length) and
            # between (self.pos, self.buffer_size)
            first_range_end = self._pos - sequence_length + 1
            # end of the second range, if the first range is empty, then the second range ends
            # in (buffer_size + (self._pos - sequence_length + 1)), otherwise the sequence will contain
            # invalid values
            second_range_end = self.buffer_size if first_range_end >= 0 else self.buffer_size + first_range_end
            valid_idxes = torch.tensor(
                list(range(0, first_range_end)) + list(range(self._pos, second_range_end)),
                device=self.device,
            )
            if len(valid_idxes) < batch_dim:
                raise ValueError(
                    f"n_samples * batch size ({batch_dim}) is larger than sampleable items ({len(valid_idxes)}), check also sequence_length"
                )
            # start_idxes are the indices of the first elements of the sequences
            start_idxes = valid_idxes[torch.randint(0, len(valid_idxes), size=(batch_dim,), device=self.device)]
        else:
            # when the buffer is not full, we need to start the sequence so that it does not go out of bounds
            start_idxes = torch.randint(0, self._pos - sequence_length + 1, size=(batch_dim,), device=self.device)

        # chunk_length contains the relative indices of the sequence (0, 1, ..., sequence_length-1)
        chunk_length = torch.arange(sequence_length, device=self.device).reshape(1, -1)
        idxes = (start_idxes.reshape(-1, 1) + chunk_length) % self.buffer_size

        # (n_samples, sequence_length, batch_size)
        sample = self._get_samples(idxes).reshape(n_samples, batch_size, sequence_length).permute(0, -1, -2)
        if clone:
            return sample.clone()
        return sample

    def _get_samples(self, batch_idxes: Tensor, sample_next_obs: bool = False) -> TensorDictBase:
        """Retrieves the items and return the TensorDict of sampled items.

        Args:
            batch_idxes (Tensor): the indices to retrieve of dimension (batch_dim, sequence_length).
            sample_next_obs (bool): whether to sample the next observations from the 'observations' key.
                Defaults to False.

        Returns:
            TensorDictBase: the sampled TensorDictBase with a `batch_size` of [batch_dim, sequence_length]
        """
        unflatten_shape = batch_idxes.shape
        # each sequence must come from the same environment
        env_idxes = (
            torch.randint(0, self.n_envs, size=(unflatten_shape[0],)).view(-1, 1).repeat(1, unflatten_shape[1]).view(-1)
        )
        # retrieve the items by flattening the indices
        # (b1_s1, b1_s2, b1_s3, ..., bn_s1, bn_s2, bn_s3, ...)
        # where bm_sk is the k-th elements in the sequence of the m-th batch
        sample = self._buf[batch_idxes.flatten(), env_idxes]
        # properly reshape the items:
        # [
        #   [b1_s1, b1_s2, ...],
        #   [b2_s1, b2_s2, ...],
        #   ...,
        #   [bn_s1, bn_s2, ...]
        # ]
        return sample.view(*unflatten_shape)
