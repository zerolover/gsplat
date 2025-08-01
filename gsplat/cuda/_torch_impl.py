import struct
from typing import Optional, Tuple

import math
import torch
import torch.nn.functional as F
from torch import Tensor
from typing_extensions import Literal, assert_never


def _quat_to_rotmat(quats: Tensor) -> Tensor:
    """Convert quaternion to rotation matrix."""
    quats = F.normalize(quats, p=2, dim=-1)
    w, x, y, z = torch.unbind(quats, dim=-1)
    R = torch.stack(
        [
            1 - 2 * (y**2 + z**2),
            2 * (x * y - w * z),
            2 * (x * z + w * y),
            2 * (x * y + w * z),
            1 - 2 * (x**2 + z**2),
            2 * (y * z - w * x),
            2 * (x * z - w * y),
            2 * (y * z + w * x),
            1 - 2 * (x**2 + y**2),
        ],
        dim=-1,
    )
    return R.reshape(quats.shape[:-1] + (3, 3))


def _quat_scale_to_matrix(
    quats: Tensor,  # [..., 4],
    scales: Tensor,  # [..., 3],
) -> Tensor:
    """Convert quaternion and scale to a 3x3 matrix (R * S)."""
    batch_dims = quats.shape[:-1]
    assert quats.shape == batch_dims + (4,), quats.shape
    assert scales.shape == batch_dims + (3,), scales.shape
    R = _quat_to_rotmat(quats)  # [..., 3, 3]
    M = R * scales[..., None, :]  # [..., 3, 3]
    return M


def _quat_scale_to_covar_preci(
    quats: Tensor,  # [..., 4],
    scales: Tensor,  # [..., 3],
    compute_covar: bool = True,
    compute_preci: bool = True,
    triu: bool = False,
) -> Tuple[Optional[Tensor], Optional[Tensor]]:
    """PyTorch implementation of `gsplat.cuda._wrapper.quat_scale_to_covar_preci()`."""
    batch_dims = quats.shape[:-1]
    assert quats.shape == batch_dims + (4,), quats.shape
    assert scales.shape == batch_dims + (3,), scales.shape
    R = _quat_to_rotmat(quats)  # [..., 3, 3]

    if compute_covar:
        M = R * scales[..., None, :]  # [..., 3, 3]
        covars = torch.einsum("...ij,...kj -> ...ik", M, M)  # [..., 3, 3]
        if triu:
            covars = covars.reshape(batch_dims + (9,))  # [..., 9]
            covars = (
                covars[..., [0, 1, 2, 4, 5, 8]] + covars[..., [0, 3, 6, 4, 7, 8]]
            ) / 2.0  # [..., 6]
    if compute_preci:
        P = R * (1 / scales[..., None, :])  # [..., 3, 3]
        precis = torch.einsum("...ij,...kj -> ...ik", P, P)  # [..., 3, 3]
        if triu:
            precis = precis.reshape(batch_dims + (9,))  # [..., 9]
            precis = (
                precis[..., [0, 1, 2, 4, 5, 8]] + precis[..., [0, 3, 6, 4, 7, 8]]
            ) / 2.0  # [..., 6]

    return covars if compute_covar else None, precis if compute_preci else None


def _persp_proj(
    means: Tensor,  # [..., C, N, 3]
    covars: Tensor,  # [..., C, N, 3, 3]
    Ks: Tensor,  # [..., C, 3, 3]
    width: int,
    height: int,
) -> Tuple[Tensor, Tensor]:
    """PyTorch implementation of perspective projection for 3D Gaussians.

    Args:
        means: Gaussian means in camera coordinate system. [..., C, N, 3].
        covars: Gaussian covariances in camera coordinate system. [..., C, N, 3, 3].
        Ks: Camera intrinsics. [..., C, 3, 3].
        width: Image width.
        height: Image height.

    Returns:
        A tuple:

        - **means2d**: Projected means. [..., C, N, 2].
        - **cov2d**: Projected covariances. [..., C, N, 2, 2].
    """
    batch_dims = means.shape[:-3]
    C, N = means.shape[-3:-1]
    assert means.shape == batch_dims + (C, N, 3), means.shape
    assert covars.shape == batch_dims + (C, N, 3, 3), covars.shape
    assert Ks.shape == batch_dims + (C, 3, 3), Ks.shape

    tx, ty, tz = torch.unbind(means, dim=-1)  # [..., C, N]
    tz2 = tz**2  # [..., C, N]

    fx = Ks[..., 0, 0, None]  # [..., C, 1]
    fy = Ks[..., 1, 1, None]  # [..., C, 1]
    cx = Ks[..., 0, 2, None]  # [..., C, 1]
    cy = Ks[..., 1, 2, None]  # [..., C, 1]
    tan_fovx = 0.5 * width / fx  # [..., C, 1]
    tan_fovy = 0.5 * height / fy  # [..., C, 1]

    lim_x_pos = (width - cx) / fx + 0.3 * tan_fovx
    lim_x_neg = cx / fx + 0.3 * tan_fovx
    lim_y_pos = (height - cy) / fy + 0.3 * tan_fovy
    lim_y_neg = cy / fy + 0.3 * tan_fovy
    tx = tz * torch.clamp(tx / tz, min=-lim_x_neg, max=lim_x_pos)
    ty = tz * torch.clamp(ty / tz, min=-lim_y_neg, max=lim_y_pos)

    O = torch.zeros(batch_dims + (C, N), device=means.device, dtype=means.dtype)
    J = torch.stack(
        [fx / tz, O, -fx * tx / tz2, O, fy / tz, -fy * ty / tz2], dim=-1
    ).reshape(batch_dims + (C, N, 2, 3))

    cov2d = torch.einsum("...ij,...jk,...kl->...il", J, covars, J.transpose(-1, -2))
    means2d = torch.einsum(
        "...ij,...nj->...ni", Ks[..., :2, :3], means
    )  # [..., C, N, 2]
    means2d = means2d / tz[..., None]  # [..., C, N, 2]
    return means2d, cov2d  # [..., C, N, 2], [..., C, N, 2, 2]


def _fisheye_proj(
    means: Tensor,  # [..., C, N, 3]
    covars: Tensor,  # [..., C, N, 3, 3]
    Ks: Tensor,  # [..., C, 3, 3]
    width: int,
    height: int,
) -> Tuple[Tensor, Tensor]:
    """PyTorch implementation of fisheye projection for 3D Gaussians.

    Args:
        means: Gaussian means in camera coordinate system. [..., C, N, 3].
        covars: Gaussian covariances in camera coordinate system. [..., C, N, 3, 3].
        Ks: Camera intrinsics. [..., C, 3, 3].
        width: Image width.
        height: Image height.

    Returns:
        A tuple:

        - **means2d**: Projected means. [..., C, N, 2].
        - **cov2d**: Projected covariances. [..., C, N, 2, 2].
    """
    batch_dims = means.shape[:-3]
    C, N = means.shape[-3:-1]
    assert means.shape == batch_dims + (C, N, 3), means.shape
    assert covars.shape == batch_dims + (C, N, 3, 3), covars.shape
    assert Ks.shape == batch_dims + (C, 3, 3), Ks.shape

    x, y, z = torch.unbind(means, dim=-1)  # [..., C, N]

    fx = Ks[..., 0, 0, None]  # [..., C, 1]
    fy = Ks[..., 1, 1, None]  # [..., C, 1]
    cx = Ks[..., 0, 2, None]  # [..., C, 1]
    cy = Ks[..., 1, 2, None]  # [..., C, 1]

    eps = 0.0000001
    xy_len = (x**2 + y**2) ** 0.5 + eps
    theta = torch.atan2(xy_len, z + eps)
    means2d = torch.stack(
        [
            x * fx * theta / xy_len + cx,
            y * fy * theta / xy_len + cy,
        ],
        dim=-1,
    )  # [..., C, N, 2]

    x2 = x * x + eps
    y2 = y * y
    xy = x * y
    x2y2 = x2 + y2
    x2y2z2_inv = 1.0 / (x2y2 + z * z)
    b = torch.atan2(xy_len, z) / xy_len / x2y2
    a = z * x2y2z2_inv / (x2y2)
    J = torch.stack(
        [
            fx * (x2 * a + y2 * b),
            fx * xy * (a - b),
            -fx * x * x2y2z2_inv,
            fy * xy * (a - b),
            fy * (y2 * a + x2 * b),
            -fy * y * x2y2z2_inv,
        ],
        dim=-1,
    ).reshape(batch_dims + (C, N, 2, 3))

    cov2d = torch.einsum("...ij,...jk,...kl->...il", J, covars, J.transpose(-1, -2))
    return means2d, cov2d  # [..., C, N, 2], [..., C, N, 2, 2]


def _ortho_proj(
    means: Tensor,  # [..., C, N, 3]
    covars: Tensor,  # [..., C, N, 3, 3]
    Ks: Tensor,  # [..., C, 3, 3]
    width: int,
    height: int,
) -> Tuple[Tensor, Tensor]:
    """PyTorch implementation of orthographic projection for 3D Gaussians.

    Args:
        means: Gaussian means in camera coordinate system. [..., C, N, 3].
        covars: Gaussian covariances in camera coordinate system. [..., C, N, 3, 3].
        Ks: Camera intrinsics. [..., C, 3, 3].
        width: Image width.
        height: Image height.

    Returns:
        A tuple:

        - **means2d**: Projected means. [..., C, N, 2].
        - **cov2d**: Projected covariances. [..., C, N, 2, 2].
    """
    batch_dims = means.shape[:-3]
    C, N = means.shape[-3:-1]
    assert means.shape == batch_dims + (C, N, 3), means.shape
    assert covars.shape == batch_dims + (C, N, 3, 3), covars.shape
    assert Ks.shape == batch_dims + (C, 3, 3), Ks.shape

    fx = Ks[..., 0, 0, None]  # [..., C, 1]
    fy = Ks[..., 1, 1, None]  # [..., C, 1]

    O = torch.zeros(batch_dims + (C, 1), device=means.device, dtype=means.dtype)
    J = (
        torch.stack([fx, O, O, O, fy, O], dim=-1)
        .reshape(batch_dims + (C, 1, 2, 3))
        .repeat([1] * len(batch_dims) + [1, N, 1, 1])
    )

    cov2d = torch.einsum("...ij,...jk,...kl->...il", J, covars, J.transpose(-1, -2))
    means2d = (
        means[..., :2] * Ks[..., None, [0, 1], [0, 1]] + Ks[..., None, [0, 1], [2, 2]]
    )  # [..., C, N, 2]
    return means2d, cov2d  # [..., C, N, 2], [..., C, N, 2, 2]


def _world_to_cam(
    means: Tensor,  # [..., N, 3]
    covars: Tensor,  # [..., N, 3, 3]
    viewmats: Tensor,  # [..., C, 4, 4]
) -> Tuple[Tensor, Tensor]:
    """PyTorch implementation of world to camera transformation on Gaussians.

    Args:
        means: Gaussian means in world coordinate system. [..., N, 3].
        covars: Gaussian covariances in world coordinate system. [..., N, 3, 3].
        viewmats: world to camera transformation matrices. [..., C, 4, 4].

    Returns:
        A tuple:

        - **means_c**: Gaussian means in camera coordinate system. [..., C, N, 3].
        - **covars_c**: Gaussian covariances in camera coordinate system. [..., C, N, 3, 3].
    """
    batch_dims = means.shape[:-2]
    N = means.shape[-2]
    C = viewmats.shape[-3]
    assert means.shape == batch_dims + (N, 3), means.shape
    assert covars.shape == batch_dims + (N, 3, 3), covars.shape
    assert viewmats.shape == batch_dims + (C, 4, 4), viewmats.shape

    R = viewmats[..., :3, :3]  # [..., C, 3, 3]
    t = viewmats[..., :3, 3]  # [..., C, 3]
    means_c = (
        torch.einsum("...cij,...nj->...cni", R, means) + t[..., None, :]
    )  # [..., C, N, 3]
    covars_c = torch.einsum(
        "...cij,...njk,...clk->...cnil", R, covars, R
    )  # [..., C, N, 3, 3]
    return means_c, covars_c


def _fully_fused_projection(
    means: Tensor,  # [..., N, 3]
    covars: Tensor,  # [..., N, 3, 3]
    viewmats: Tensor,  # [..., C, 4, 4]
    Ks: Tensor,  # [..., C, 3, 3]
    width: int,
    height: int,
    eps2d: float = 0.3,
    near_plane: float = 0.01,
    far_plane: float = 1e10,
    calc_compensations: bool = False,
    camera_model: Literal["pinhole", "ortho", "fisheye", "ftheta"] = "pinhole",
) -> Tuple[Tensor, Tensor, Tensor, Tensor, Optional[Tensor]]:
    """PyTorch implementation of `gsplat.cuda._wrapper.fully_fused_projection()`

    .. note::

        This is a minimal implementation of fully fused version, which has more
        arguments. Not all arguments are supported.
    """
    batch_dims = means.shape[:-2]
    N = means.shape[-2]
    C = viewmats.shape[-3]
    assert means.shape == batch_dims + (N, 3), means.shape
    assert covars.shape == batch_dims + (N, 3, 3), covars.shape
    assert viewmats.shape == batch_dims + (C, 4, 4), viewmats.shape
    assert Ks.shape == batch_dims + (C, 3, 3), Ks.shape

    assert (
        camera_model != "ftheta"
    ), "ftheta camera is only supported via UT, please set with_ut=True in the rasterization()"

    means_c, covars_c = _world_to_cam(means, covars, viewmats)

    if camera_model == "ortho":
        means2d, covars2d = _ortho_proj(means_c, covars_c, Ks, width, height)
    elif camera_model == "fisheye":
        means2d, covars2d = _fisheye_proj(means_c, covars_c, Ks, width, height)
    elif camera_model == "pinhole":
        means2d, covars2d = _persp_proj(means_c, covars_c, Ks, width, height)
    else:
        assert_never(camera_model)

    det_orig = (
        covars2d[..., 0, 0] * covars2d[..., 1, 1]
        - covars2d[..., 0, 1] * covars2d[..., 1, 0]
    )
    covars2d = covars2d + torch.eye(2, device=means.device, dtype=means.dtype) * eps2d

    det = (
        covars2d[..., 0, 0] * covars2d[..., 1, 1]
        - covars2d[..., 0, 1] * covars2d[..., 1, 0]
    )
    det = det.clamp(min=1e-10)

    if calc_compensations:
        compensations = torch.sqrt(torch.clamp(det_orig / det, min=0.0))
    else:
        compensations = None

    conics = torch.stack(
        [
            covars2d[..., 1, 1] / det,
            -(covars2d[..., 0, 1] + covars2d[..., 1, 0]) / 2.0 / det,
            covars2d[..., 0, 0] / det,
        ],
        dim=-1,
    )  # [..., C, N, 3]

    depths = means_c[..., 2]  # [..., C, N]

    radius_x = torch.ceil(3.33 * torch.sqrt(covars2d[..., 0, 0]))
    radius_y = torch.ceil(3.33 * torch.sqrt(covars2d[..., 1, 1]))

    radius = torch.stack([radius_x, radius_y], dim=-1)  # [..., C, N, 2]

    valid = (det > 0) & (depths > near_plane) & (depths < far_plane)
    radius[~valid] = 0.0

    inside = (
        (means2d[..., 0] + radius[..., 0] > 0)
        & (means2d[..., 0] - radius[..., 0] < width)
        & (means2d[..., 1] + radius[..., 1] > 0)
        & (means2d[..., 1] - radius[..., 1] < height)
    )
    radius[~inside] = 0.0

    radii = radius.int()
    return radii, means2d, depths, conics, compensations


@torch.no_grad()
def _isect_tiles(
    means2d: Tensor,  # [..., N, 2]
    radii: Tensor,  # [..., N, 2]
    depths: Tensor,  # [..., N]
    tile_size: int,
    tile_width: int,
    tile_height: int,
    sort: bool = True,
) -> Tuple[Tensor, Tensor, Tensor]:
    """Pytorch implementation of `gsplat.cuda._wrapper.isect_tiles()`.

    .. note::

        This is a minimal implementation of the fully fused version, which has more
        arguments. Not all arguments are supported.
    """
    image_dims = means2d.shape[:-2]
    N = means2d.shape[-2]
    assert means2d.shape == image_dims + (N, 2), means2d.shape
    assert radii.shape == image_dims + (N, 2), radii.shape
    assert depths.shape == image_dims + (N,), depths.shape

    device = means2d.device
    I = math.prod(image_dims)
    means2d = means2d.reshape(I, N, 2)
    radii = radii.reshape(I, N, 2)
    depths = depths.reshape(I, N)

    # compute tiles_per_gauss
    tile_means2d = means2d / tile_size
    tile_radii = radii / tile_size
    tile_mins = torch.floor(tile_means2d - tile_radii).int()
    tile_maxs = torch.ceil(tile_means2d + tile_radii).int()
    tile_mins[..., 0] = torch.clamp(tile_mins[..., 0], 0, tile_width)
    tile_mins[..., 1] = torch.clamp(tile_mins[..., 1], 0, tile_height)
    tile_maxs[..., 0] = torch.clamp(tile_maxs[..., 0], 0, tile_width)
    tile_maxs[..., 1] = torch.clamp(tile_maxs[..., 1], 0, tile_height)
    tiles_per_gauss = (tile_maxs - tile_mins).prod(dim=-1)  # [..., C, N]
    tiles_per_gauss *= (radii > 0.0).all(dim=-1)

    n_isects = tiles_per_gauss.sum().item()
    # store in two int32 tensors instead, otherwise it will trigger out of bounds error
    # isect_ids = torch.empty(n_isects, dtype=torch.int64, device=device)
    isect_ids_lo = torch.empty(n_isects, dtype=torch.int32, device=device)
    isect_ids_hi = torch.empty(n_isects, dtype=torch.int32, device=device)
    flatten_ids = torch.empty(n_isects, dtype=torch.int32, device=device)

    cum_tiles_per_gauss = torch.cumsum(tiles_per_gauss.flatten(), dim=0)
    image_n_bits = I.bit_length()
    tile_n_bits = (tile_width * tile_height).bit_length()
    assert image_n_bits + tile_n_bits + 32 <= 64

    def binary(num):
        return "".join("{:0>8b}".format(c) for c in struct.pack("!f", num))

    def kernel(image_id, gauss_id):
        if radii[image_id, gauss_id, 0] <= 0.0 or radii[image_id, gauss_id, 1] <= 0.0:
            return
        index = image_id * N + gauss_id
        curr_idx = cum_tiles_per_gauss[index - 1] if index > 0 else 0

        # Reinterpret float bits as int32 (preserving bit pattern)
        depth_f32 = depths[image_id, gauss_id]
        depth_id = struct.unpack("i", struct.pack("f", depth_f32))[0]
        # Store in a 64-bit int, zero-extending to lower 32 bits
        depth_id = int(depth_id) & 0xFFFFFFFF  # Ensures upper 32 bits are zero

        tile_min = tile_mins[image_id, gauss_id]
        tile_max = tile_maxs[image_id, gauss_id]
        for y in range(tile_min[1], tile_max[1]):
            for x in range(tile_min[0], tile_max[0]):
                tile_id = y * tile_width + x
                # isect_ids[curr_idx] = (
                #     (image_id << (tile_n_bits + 32))
                #     | (tile_id << 32)
                #     | depth_id
                # )
                isect_ids_lo[curr_idx] = depth_id
                isect_ids_hi[curr_idx] = (image_id << tile_n_bits) | tile_id
                flatten_ids[curr_idx] = index  # flattened index
                curr_idx += 1

    for image_id in range(I):
        for gauss_id in range(N):
            kernel(image_id, gauss_id)

    isect_ids = (isect_ids_hi.to(torch.int64) << 32) | (
        isect_ids_lo.to(torch.int64) & 0xFFFFFFFF
    )

    if sort:
        isect_ids, sort_indices = torch.sort(isect_ids)
        flatten_ids = flatten_ids[sort_indices]

    tiles_per_gauss = tiles_per_gauss.reshape(image_dims + (N,)).int()
    return tiles_per_gauss, isect_ids, flatten_ids


@torch.no_grad()
def _isect_offset_encode(
    isect_ids: Tensor, I: int, tile_width: int, tile_height: int
) -> Tensor:
    """Pytorch implementation of `gsplat.cuda._wrapper.isect_offset_encode()`.

    .. note::

        This is a minimal implementation of the fully fused version, which has more
        arguments. Not all arguments are supported.
    """
    tile_n_bits = (tile_width * tile_height).bit_length()
    tile_counts = torch.zeros(
        (I, tile_height, tile_width), dtype=torch.int64, device=isect_ids.device
    )

    isect_ids_uq, counts = torch.unique_consecutive(isect_ids >> 32, return_counts=True)

    image_ids_uq = isect_ids_uq >> tile_n_bits
    tile_ids_uq = isect_ids_uq & ((1 << tile_n_bits) - 1)
    tile_ids_x_uq = tile_ids_uq % tile_width
    tile_ids_y_uq = tile_ids_uq // tile_width

    tile_counts[image_ids_uq, tile_ids_y_uq, tile_ids_x_uq] = counts

    cum_tile_counts = torch.cumsum(tile_counts.flatten(), dim=0).reshape_as(tile_counts)
    offsets = cum_tile_counts - tile_counts
    return offsets.int()


def accumulate(
    means2d: Tensor,  # [..., N, 2]
    conics: Tensor,  # [..., N, 3]
    opacities: Tensor,  # [..., N]
    colors: Tensor,  # [..., N, channels]
    gaussian_ids: Tensor,  # [M]
    pixel_ids: Tensor,  # [M]
    image_ids: Tensor,  # [M]
    image_width: int,
    image_height: int,
) -> Tuple[Tensor, Tensor]:
    """Alpah compositing of 2D Gaussians in Pure Pytorch.

    This function performs alpha compositing for Gaussians based on the pair of indices
    {gaussian_ids, pixel_ids, image_ids}, which annotates the intersection between all
    pixels and Gaussians. These intersections can be accquired from
    `gsplat.rasterize_to_indices_in_range`.

    .. note::

        This function exposes the alpha compositing process into pure Pytorch.
        So it relies on Pytorch's autograd for the backpropagation. It is much slower
        than our fully fused rasterization implementation and comsumes much more GPU memory.
        But it could serve as a playground for new ideas or debugging, as no backward
        implementation is needed.

    .. warning::

        This function requires the `nerfacc` package to be installed. Please install it
        using the following command `pip install nerfacc`.

    Args:
        means2d: Gaussian means in 2D. [..., N, 2]
        conics: Inverse of the 2D Gaussian covariance, Only upper triangle values. [..., N, 3]
        opacities: Per-view Gaussian opacities (for example, when antialiasing is
            enabled, Gaussian in each view would efficiently have different opacity). [..., N]
        colors: Per-view Gaussian colors. Supports N-D features. [..., N, channels]
        gaussian_ids: Collection of Gaussian indices to be rasterized. A flattened list of shape [M].
        pixel_ids: Collection of pixel indices (row-major) to be rasterized. A flattened list of shape [M].
        image_ids: Collection of image indices to be rasterized. A flattened list of shape [M].
        image_width: Image width.
        image_height: Image height.

    Returns:
        A tuple:

        - **renders**: Accumulated colors. [..., image_height, image_width, channels]
        - **alphas**: Accumulated opacities. [..., image_height, image_width, 1]
    """

    try:
        from nerfacc import accumulate_along_rays, render_weight_from_alpha
    except ImportError:
        raise ImportError("Please install nerfacc package: pip install nerfacc")

    image_dims = means2d.shape[:-2]
    I = math.prod(image_dims)
    N = means2d.shape[-2]
    channels = colors.shape[-1]
    assert means2d.shape == image_dims + (N, 2), means2d.shape
    assert conics.shape == image_dims + (N, 3), conics.shape
    assert opacities.shape == image_dims + (N,), opacities.shape
    assert colors.shape == image_dims + (N, channels), colors.shape

    means2d = means2d.reshape(I, N, 2)
    conics = conics.reshape(I, N, 3)
    opacities = opacities.reshape(I, N)
    colors = colors.reshape(I, N, channels)

    pixel_ids_x = pixel_ids % image_width
    pixel_ids_y = pixel_ids // image_width
    pixel_coords = torch.stack([pixel_ids_x, pixel_ids_y], dim=-1) + 0.5  # [M, 2]
    deltas = pixel_coords - means2d[image_ids, gaussian_ids]  # [M, 2]
    c = conics[image_ids, gaussian_ids]  # [M, 3]
    sigmas = (
        0.5 * (c[:, 0] * deltas[:, 0] ** 2 + c[:, 2] * deltas[:, 1] ** 2)
        + c[:, 1] * deltas[:, 0] * deltas[:, 1]
    )  # [M]
    alphas = torch.clamp_max(
        opacities[image_ids, gaussian_ids] * torch.exp(-sigmas), 0.999
    )

    indices = image_ids * image_height * image_width + pixel_ids
    total_pixels = I * image_height * image_width

    weights, trans = render_weight_from_alpha(
        alphas, ray_indices=indices, n_rays=total_pixels
    )
    renders = accumulate_along_rays(
        weights,
        colors[image_ids, gaussian_ids],
        ray_indices=indices,
        n_rays=total_pixels,
    ).reshape(image_dims + (image_height, image_width, channels))
    alphas = accumulate_along_rays(
        weights, None, ray_indices=indices, n_rays=total_pixels
    ).reshape(image_dims + (image_height, image_width, 1))

    return renders, alphas


def _rasterize_to_pixels(
    means2d: Tensor,  # [..., N, 2]
    conics: Tensor,  # [..., N, 3]
    colors: Tensor,  # [..., N, channels]
    opacities: Tensor,  # [..., N]
    image_width: int,
    image_height: int,
    tile_size: int,
    isect_offsets: Tensor,  # [..., tile_height, tile_width]
    flatten_ids: Tensor,  # [n_isects]
    backgrounds: Optional[Tensor] = None,  # [..., channels]
    batch_per_iter: int = 100,
):
    """Pytorch implementation of `gsplat.cuda._wrapper.rasterize_to_pixels()`.

    This function rasterizes 2D Gaussians to pixels in a Pytorch-friendly way. It
    iteratively accumulates the renderings within each batch of Gaussians. The
    interations are controlled by `batch_per_iter`.

    .. note::
        This is a minimal implementation of the fully fused version, which has more
        arguments. Not all arguments are supported.

    .. note::

        This function relies on Pytorch's autograd for the backpropagation. It is much slower
        than our fully fused rasterization implementation and comsumes much more GPU memory.
        But it could serve as a playground for new ideas or debugging, as no backward
        implementation is needed.

    .. warning::

        This function requires the `nerfacc` package to be installed. Please install it
        using the following command `pip install nerfacc`.
    """
    from ._wrapper import rasterize_to_indices_in_range

    image_dims = means2d.shape[:-2]
    channels = colors.shape[-1]
    N = means2d.shape[-2]
    tile_height = isect_offsets.shape[-2]
    tile_width = isect_offsets.shape[-1]

    assert means2d.shape == image_dims + (N, 2), means2d.shape
    assert conics.shape == image_dims + (N, 3), conics.shape
    assert colors.shape == image_dims + (N, channels), colors.shape
    assert opacities.shape == image_dims + (N,), opacities.shape
    assert isect_offsets.shape == image_dims + (
        tile_height,
        tile_width,
    ), isect_offsets.shape
    n_isects = len(flatten_ids)
    device = means2d.device

    render_colors = torch.zeros(
        image_dims + (image_height, image_width, channels), device=device
    )
    render_alphas = torch.zeros(
        image_dims + (image_height, image_width, 1), device=device
    )

    # Split Gaussians into batches and iteratively accumulate the renderings
    block_size = tile_size * tile_size
    isect_offsets_fl = torch.cat(
        [isect_offsets.flatten(), torch.tensor([n_isects], device=device)]
    )
    max_range = (isect_offsets_fl[1:] - isect_offsets_fl[:-1]).max().item()
    num_batches = (max_range + block_size - 1) // block_size
    for step in range(0, num_batches, batch_per_iter):
        transmittances = 1.0 - render_alphas[..., 0]

        # Find the M intersections between pixels and gaussians.
        # Each intersection corresponds to a tuple (gs_id, pixel_id, image_id)
        gs_ids, pixel_ids, image_ids = rasterize_to_indices_in_range(
            step,
            step + batch_per_iter,
            transmittances,
            means2d,
            conics,
            opacities,
            image_width,
            image_height,
            tile_size,
            isect_offsets,
            flatten_ids,
        )  # [M], [M], [M]
        if len(gs_ids) == 0:
            break

        # Accumulate the renderings within this batch of Gaussians.
        renders_step, accs_step = accumulate(
            means2d,
            conics,
            opacities,
            colors,
            gs_ids,
            pixel_ids,
            image_ids,
            image_width,
            image_height,
        )
        render_colors = render_colors + renders_step * transmittances[..., None]
        render_alphas = render_alphas + accs_step * transmittances[..., None]

    render_alphas = render_alphas
    if backgrounds is not None:
        render_colors = render_colors + backgrounds[..., None, None, :] * (
            1.0 - render_alphas
        )

    return render_colors, render_alphas


def _eval_sh_bases_fast(basis_dim: int, dirs: Tensor):
    """
    Evaluate spherical harmonics bases at unit direction for high orders
    using approach described by
    Efficient Spherical Harmonic Evaluation, Peter-Pike Sloan, JCGT 2013
    https://jcgt.org/published/0002/02/06/


    :param basis_dim: int SH basis dim. Currently, only 1-25 square numbers supported
    :param dirs: torch.Tensor (..., 3) unit directions

    :return: torch.Tensor (..., basis_dim)

    See reference C++ code in https://jcgt.org/published/0002/02/06/code.zip
    """
    result = torch.empty(
        (*dirs.shape[:-1], basis_dim), dtype=dirs.dtype, device=dirs.device
    )

    result[..., 0] = 0.2820947917738781

    if basis_dim <= 1:
        return result

    x, y, z = dirs.unbind(-1)

    fTmpA = -0.48860251190292
    result[..., 2] = -fTmpA * z
    result[..., 3] = fTmpA * x
    result[..., 1] = fTmpA * y

    if basis_dim <= 4:
        return result

    z2 = z * z
    fTmpB = -1.092548430592079 * z
    fTmpA = 0.5462742152960395
    fC1 = x * x - y * y
    fS1 = 2 * x * y
    result[..., 6] = 0.9461746957575601 * z2 - 0.3153915652525201
    result[..., 7] = fTmpB * x
    result[..., 5] = fTmpB * y
    result[..., 8] = fTmpA * fC1
    result[..., 4] = fTmpA * fS1

    if basis_dim <= 9:
        return result

    fTmpC = -2.285228997322329 * z2 + 0.4570457994644658
    fTmpB = 1.445305721320277 * z
    fTmpA = -0.5900435899266435
    fC2 = x * fC1 - y * fS1
    fS2 = x * fS1 + y * fC1
    result[..., 12] = z * (1.865881662950577 * z2 - 1.119528997770346)
    result[..., 13] = fTmpC * x
    result[..., 11] = fTmpC * y
    result[..., 14] = fTmpB * fC1
    result[..., 10] = fTmpB * fS1
    result[..., 15] = fTmpA * fC2
    result[..., 9] = fTmpA * fS2

    if basis_dim <= 16:
        return result

    fTmpD = z * (-4.683325804901025 * z2 + 2.007139630671868)
    fTmpC = 3.31161143515146 * z2 - 0.47308734787878
    fTmpB = -1.770130769779931 * z
    fTmpA = 0.6258357354491763
    fC3 = x * fC2 - y * fS2
    fS3 = x * fS2 + y * fC2
    result[..., 20] = 1.984313483298443 * z2 * (
        1.865881662950577 * z2 - 1.119528997770346
    ) + -1.006230589874905 * (0.9461746957575601 * z2 - 0.3153915652525201)
    result[..., 21] = fTmpD * x
    result[..., 19] = fTmpD * y
    result[..., 22] = fTmpC * fC1
    result[..., 18] = fTmpC * fS1
    result[..., 23] = fTmpB * fC2
    result[..., 17] = fTmpB * fS2
    result[..., 24] = fTmpA * fC3
    result[..., 16] = fTmpA * fS3
    return result


def _spherical_harmonics(
    degrees_to_use: int,
    dirs: torch.Tensor,  # [..., 3]
    coeffs: torch.Tensor,  # [..., K, 3]
):
    """Pytorch implementation of `gsplat.cuda._wrapper.spherical_harmonics()`."""
    assert (degrees_to_use + 1) ** 2 <= coeffs.shape[-2], coeffs.shape
    batch_dims = dirs.shape[:-1]
    assert dirs.shape == batch_dims + (3,), dirs.shape
    assert (
        (len(coeffs.shape) == len(batch_dims) + 2)
        and coeffs.shape[:-2] == batch_dims
        and coeffs.shape[-1] == 3
    ), coeffs.shape
    dirs = F.normalize(dirs, p=2, dim=-1)
    num_bases = (degrees_to_use + 1) ** 2
    bases = torch.zeros_like(coeffs[..., 0])
    bases[..., :num_bases] = _eval_sh_bases_fast(num_bases, dirs)
    return (bases[..., None] * coeffs).sum(dim=-2)
