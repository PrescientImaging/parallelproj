"""
OSEM with projection data
=========================

This example demonstrates the use of the MLEM algorithm to minimize the negative Poisson log-likelihood function.

.. math::
    f(x) = \sum_{i=1}^m \\bar{y}_i - \\bar{y}_i (x) \log(y_i)

using the linear forward model

.. math::
    \\bar{y}(x) = A x + s

.. tip::
    parallelproj is python array API compatible meaning it supports different 
    array backends (e.g. numpy, cupy, torch, ...) and devices (CPU or GPU).
    Choose your preferred array API ``xp`` and device ``dev`` below.
"""
# %%
from __future__ import annotations
from numpy.array_api._array_object import Array

import array_api_compat.numpy as xp

# import array_api_compat.cupy as xp
# import array_api_compat.torch as xp

import parallelproj
from array_api_compat import to_device
import array_api_compat.numpy as np
import matplotlib.pyplot as plt
import matplotlib.animation as animation

# choose a device (CPU or CUDA GPU)
if "numpy" in xp.__name__:
    # using numpy, device must be cpu
    dev = "cpu"
elif "cupy" in xp.__name__:
    # using cupy, only cuda devices are possible
    dev = xp.cuda.Device(0)
elif "torch" in xp.__name__:
    # using torch valid choices are 'cpu' or 'cuda'
    if parallelproj.cuda_present:
        dev = "cuda"
    else:
        dev = "cpu"


# %%
# Setup of the forward model :math:`\bar{y}(x) = A x + s`
# --------------------------------------------------------
#
# We setup a linear forward operator :math:`A` consisting of an
# image-based resolution model, a non-TOF PET projector and an attenuation model
#
# .. note::
#     The OSEM implementation below works with all linear operators that
#     subclass :class:`.LinearOperator` (e.g. the high-level projectors).

num_rings = 5
scanner = parallelproj.RegularPolygonPETScannerGeometry(
    xp,
    dev,
    radius=65.0,
    num_sides=12,
    num_lor_endpoints_per_side=15,
    lor_spacing=2.3,
    ring_positions=xp.linspace(-10, 10, num_rings),
    symmetry_axis=2,
)

# %%
# setup the LOR descriptor that defines the sinogram

img_shape = (40, 40, 8)
voxel_size = (2.0, 2.0, 2.0)

lor_desc = parallelproj.RegularPolygonPETLORDescriptor(
    scanner,
    radial_trim=10,
    max_ring_difference=2,
    sinogram_order=parallelproj.SinogramSpatialAxisOrder.RVP,
)

proj = parallelproj.RegularPolygonPETProjector(
    lor_desc, img_shape=img_shape, voxel_size=voxel_size
)

# setup a simple test image containing a few "hot rods"
x_true = xp.ones(proj.in_shape, device=dev, dtype=xp.float32)
c0 = proj.in_shape[0] // 2
c1 = proj.in_shape[1] // 2
x_true[(c0 - 2) : (c0 + 2), (c1 - 2) : (c1 + 2), :] = 5.0
x_true[4, c1, 2:] = 5.0
x_true[c0, 4, :-2] = 5.0

x_true[:2, :, :] = 0
x_true[-2:, :, :] = 0
x_true[:, :2, :] = 0
x_true[:, -2:, :] = 0

# setup an attenuation image
x_att = 0.01 * xp.astype(x_true > 0, xp.float32)
# calculate the attenuation sinogram
att_sino = xp.exp(-proj(x_att))
att_op = parallelproj.ElementwiseMultiplicationOperator(att_sino)

res_model = parallelproj.GaussianFilterOperator(
    proj.in_shape, sigma=4.5 / (2.35 * proj.voxel_size)
)

pet_lin_op = parallelproj.CompositeLinearOperator((att_op, proj, res_model))

# %%
# Simulation of projection data
# -----------------------------
#
# We setup an arbitrary ground truth :math:`x_{true}` and simulate
# noise-free and noisy data :math:`y` by adding Poisson noise.

# simulated noise-free data
noise_free_data = pet_lin_op(x_true)

# generate a contant contamination sinogram
contamination = xp.full(
    noise_free_data.shape,
    0.5 * float(xp.mean(noise_free_data)),
    device=dev,
    dtype=xp.float32,
)

noise_free_data += contamination

# add Poisson noise
np.random.seed(1)
y = xp.asarray(
    np.random.poisson(np.asarray(to_device(noise_free_data, "cpu"))),
    device=dev,
    dtype=xp.float64,
)

# %%
# Split forward model into subsets :math:`A^k`
# --------------------------------------------

num_subsets = 9

subset_nums = []
for i in range(num_subsets // 2):
    subset_nums += [x for x in range(i, num_subsets, 4)]

subset_slices = []
subset_views = []
view_axis_num = proj.lor_descriptor.view_axis_num
all_views = proj.views

for i in subset_nums:
    sl = len(proj.out_shape) * [slice(None)]
    sl[view_axis_num] = slice(i, None, num_subsets)
    sl = tuple(sl)
    subset_slices.append(sl)
    subset_views.append(all_views[sl[view_axis_num]])

pet_subset_linop_seq = parallelproj.LinearOperatorSequence(
    [
        parallelproj.CompositeLinearOperator(
            [
                parallelproj.ElementwiseMultiplicationOperator(
                    att_sino[subset_slices[i]]
                ),
                parallelproj.RegularPolygonPETProjector(
                    lor_desc, img_shape=img_shape, voxel_size=voxel_size, views=sv
                ),
                res_model,
            ]
        )
        for i, sv in enumerate(subset_views)
    ]
)

# %%
# OSEM iterations to minimize :math:`f(x)`
# ----------------------------------------
#
# We apply multiple OSEM updates
#
# .. math::
#     x^+ = \frac{x}{(A^k)^H 1} (A^k)^H \frac{y^k}{A^k x + s^k}
#
# to calculate the minimizer of :math:`f(x)` iteratively.
#
# To monitor the convergence we calculate the relative cost
#
# .. math::
#    \frac{f(x) - f(x^*)}{|f(x^*)|}
#
# and the distance to the optimal point
#
# .. math::
#    \frac{\|x - x^*\|}{\|x^*\|}.


def em_update(
    x_cur: Array,
    data: Array,
    op: parallelproj.LinearOperator,
    s: Array,
    adjoint_ones: Array,
) -> Array:
    """EM update

    Parameters
    ----------
    x_cur : Array
        current solution
    data : Array
        data
    op : parallelproj.LinearOperator
        linear forward operator
    s : Array
        contamination
    adjoint_ones : Array
        adjoint of ones

    Returns
    -------
    Array
        _description_
    """
    ybar = op(x_cur) + s
    return x * op.adjoint(data / ybar) / adjoint_ones


# %%
# number MLEM iterations
num_iter = 45 // len(pet_subset_linop_seq)

# initialize x
x = xp.ones(pet_lin_op.in_shape, dtype=xp.float64, device=dev)

# calculate A_k^H 1 for all subsets k
subset_adjoint_ones = [
    x.adjoint(xp.ones(x.out_shape, dtype=xp.float64, device=dev))
    for x in pet_subset_linop_seq
]

for i in range(num_iter):
    for k, sl in enumerate(subset_slices):
        print(f"OSEM iteration {(k+1):03} / {(i + 1):03} / {num_iter:03}", end="\r")
        x = em_update(
            x, y[sl], pet_subset_linop_seq[k], contamination[sl], subset_adjoint_ones[k]
        )

# %%
# calculate the negative Poisson log-likelihood function of the reconstruction
# ----------------------------------------------------------------------------

# calculate the negative Poisson log-likelihood function of the reconstruction
exp = pet_lin_op(x) + contamination
# calculate the relative cost and distance to the optimal point
cost = float(xp.sum(exp - y * xp.log(exp)))
print(f"\nOSEM cost {cost:.6E} after {num_iter:03} iterations")


# %%
# Visualize the results
# ---------------------


def _update_img(i):
    img0.set_data(x_true_np[:, :, i])
    img1.set_data(x_np[:, :, i])
    ax[0].set_title(f"true image - plane {i:02}")
    ax[1].set_title(f"OSEM iteration {num_iter} - {num_subsets} subsets - plane {i:02}")
    return (img0, img1)


x_true_np = np.asarray(to_device(x_true, "cpu"))
x_np = np.asarray(to_device(x, "cpu"))

fig, ax = plt.subplots(1, 2, figsize=(10, 5))
vmax = x_np.max()
img0 = ax[0].imshow(x_true_np[:, :, 0], cmap="Greys", vmin=0, vmax=vmax)
img1 = ax[1].imshow(x_np[:, :, 0], cmap="Greys", vmin=0, vmax=vmax)
ax[0].set_title(f"true image - plane {0:02}")
ax[1].set_title(f"OSEM iteration {num_iter} - {num_subsets} subsets - plane {0:02}")
fig.tight_layout()
ani = animation.FuncAnimation(fig, _update_img, x_np.shape[2], interval=200, blit=False)
fig.show()
