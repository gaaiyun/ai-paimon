import numpy as np
import torch


def maximum_path(neg_cent: torch.Tensor, mask: torch.Tensor) -> torch.Tensor:
    """Compute the monotonic alignment path (MAS).

    This is a pure-Python fallback that replaces the Cython ``core.pyx``
    implementation, so that the project works on machines without a C
    compiler (e.g. Windows without MSVC).

    Parameters
    ----------
    neg_cent : Tensor of shape ``[B, T_t, T_s]``
        Negative log-centroids.
    mask : Tensor of shape ``[B, T_t, T_s]``
        Binary alignment mask.

    Returns
    -------
    Tensor of shape ``[B, T_t, T_s]``
        One-hot alignment path.
    """
    device = neg_cent.device
    dtype = neg_cent.dtype

    b, t_t, t_s = neg_cent.shape
    neg_cent_np = neg_cent.data.cpu().numpy().astype(np.float32)
    path = np.zeros_like(neg_cent_np, dtype=np.int32)

    t_t_maxs = mask.sum(1)[:, 0].data.cpu().numpy().astype(np.int32)
    t_s_maxs = mask.sum(2)[:, 0].data.cpu().numpy().astype(np.int32)

    for i in range(b):
        _maximum_path_each(path[i], neg_cent_np[i], t_t_maxs[i], t_s_maxs[i])

    return torch.from_numpy(path).to(device=device, dtype=dtype)


def _maximum_path_each(
    path: np.ndarray,
    value: np.ndarray,
    t_y: int,
    t_x: int,
) -> None:
    """Fill *path* in-place with the optimal monotonic alignment for one
    batch element using a standard Viterbi-style DP sweep."""
    # Forward pass — accumulate
    for y in range(1, t_y):
        for x in range(max(0, t_x + y - t_y), min(t_x, y + 1)):
            if x == y:
                v_cur = value[y - 1, x - 1]
            elif x == 0:
                v_cur = value[y - 1, x]
            else:
                v_cur = max(value[y - 1, x], value[y - 1, x - 1])
            value[y, x] += v_cur

    # Backward pass — trace
    index = t_x - 1
    for y in range(t_y - 1, -1, -1):
        path[y, index] = 1
        if index != 0 and (
            index == y or value[y - 1, index - 1] > value[y - 1, index]
        ):
            index -= 1
