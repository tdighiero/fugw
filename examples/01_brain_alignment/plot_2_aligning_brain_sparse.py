# %%
"""
====================================================================
Align high-resolution brain surfaces of 2 individuals with fMRI data
====================================================================

In this example, we show how to use this package to align 2
high-resolution left hemispheres using fMRI feature maps
(z-score contrast maps).
Note that, since we want this example to run on CPU,
we stick to rather low-resolution meshes (around 10k vertices
per hemisphere) but that this package can easily scale to resolutions
above 150k vertices per hemisphere. In this case, with appropriate
hyper-parameters and solver parameters, it takes less than
10 minutes to compute a mapping between 2 such distributions
using a V100 Nvidia GPU.

**Before reading this tutorial, you should first go through**
:ref:`the example aligning brain data at a low-resolution
<sphx_glr_auto_examples_01_brain_alignment_plot_1_aligning_brain_dense.py>`,
which explains the ropes of brain alignment in more detail
than this example.
In the current example, we focus more on how to use this package
to align data using real-life resolutions.

"""

# sphinx_gallery_thumbnail_number = 8
import time

import matplotlib as mpl
import matplotlib.gridspec as gridspec
import matplotlib.pyplot as plt
import numpy as np
import torch

from fugw.mappings import FUGW, FUGWSparse
from fugw.scripts import coarse_to_fine, lmds
from mpl_toolkits.axes_grid1 import make_axes_locatable
from nilearn import datasets, image, plotting, surface
from scipy.sparse import coo_matrix

# %%
# Let's download 5 volumetric contrast maps per individual
# using ``nilearn``'s API. We will use the first 4 of them
# to compute an alignment between the source and target subjects,
# and use the left-out contrast to assess the quality of our alignment.

np.random.seed(0)
torch.manual_seed(0)

n_subjects = 2

contrasts = [
    "sentence reading vs checkerboard",
    "sentence listening",
    "calculation vs sentences",
    "left vs right button press",
    "checkerboard",
]
n_training_contrasts = 4

brain_data = datasets.fetch_localizer_contrasts(
    contrasts,
    n_subjects=n_subjects,
    get_anats=True,
)

source_imgs_paths = brain_data["cmaps"][0 : len(contrasts)]
target_imgs_paths = brain_data["cmaps"][len(contrasts) : 2 * len(contrasts)]

# %%
# Here is what the first contrast map of the source subject looks like
# (the following figure is interactive):

contrast_index = 0
plotting.view_img(
    source_imgs_paths[contrast_index],
    brain_data["anats"][0],
    title=f"Contrast {contrast_index} (source subject)",
    opacity=0.5,
)

# %%
# Computing feature arrays
# ------------------------
# Let's project these 4 maps to a mesh of the cortical surface
# and aggregate these projections to build an array of features for the
# source and target subjects.
# For the sake of keeping the training phase of our mapping short even on CPU,
# we project these volumetric maps on a low-resolution mesh
# made of 10242 vertices.

fsaverage5 = datasets.fetch_surf_fsaverage(mesh="fsaverage5")

# %%


def load_images_and_project_to_surface(image_paths):
    """Util function for loading and projecting volumetric images."""
    images = [image.load_img(img) for img in image_paths]
    surface_images = [
        np.nan_to_num(surface.vol_to_surf(img, fsaverage5.pial_left))
        for img in images
    ]

    return np.stack(surface_images)


source_features = load_images_and_project_to_surface(source_imgs_paths)
target_features = load_images_and_project_to_surface(target_imgs_paths)
source_features.shape


# %%
# Here is a figure showing the 4 projected maps for each of
# the 2 individuals:


def plot_surface_map(surface_map, cmap="coolwarm", colorbar=True, **kwargs):
    """Util function for plotting surfaces."""
    plotting.plot_surf(
        fsaverage5.pial_left,
        surface_map,
        cmap=cmap,
        colorbar=colorbar,
        bg_map=fsaverage5.sulc_left,
        bg_on_data=True,
        darkness=0.5,
        **kwargs,
    )


fig = plt.figure(figsize=(3 * n_subjects, 3 * len(contrasts)))
grid_spec = gridspec.GridSpec(len(contrasts), n_subjects, figure=fig)

# Print all feature maps
for i, contrast_name in enumerate(contrasts):
    for j, features in enumerate([source_features, target_features]):
        ax = fig.add_subplot(grid_spec[i, j], projection="3d")
        plot_surface_map(
            features[i, :], axes=ax, vmax=10, vmin=-10, colorbar=False
        )

    # Add labels to subplots
    if i == 0:
        for j in range(2):
            ax = fig.add_subplot(grid_spec[i, j])
            ax.axis("off")
            ax.text(
                0.5,
                1,
                "source subject" if j == 0 else "target subject",
                va="center",
                ha="center",
            )

    ax = fig.add_subplot(grid_spec[i, :])
    ax.axis("off")
    ax.text(0.5, 0, contrast_name, va="center", ha="center")

# Add colorbar
ax = fig.add_subplot(grid_spec[2, :])
ax.axis("off")
divider = make_axes_locatable(ax)
cax = divider.append_axes("right", size="2%")
fig.add_axes(cax)
fig.colorbar(
    mpl.cm.ScalarMappable(
        norm=mpl.colors.Normalize(vmin=-10, vmax=10), cmap="coolwarm"
    ),
    cax=cax,
)

plt.show()


# %%
# Estimating geometry kernel matrices
# -----------------------------------
# This time, let's assume matrices of size ``(n, n)`` (or ``(n, m)``,
# ``(m, m)``) won't fit in memory, even with a powerful GPU -
# as a rule of thumb, this will typically be the case if ``n`` is greater
# than 50k. Therefore, the source and target geometry matrices cannot be
# explicitely computed. Instead, we derive an embedding ``X`` in dimension
# ``k`` to approximate these matrices. Under the hood, this embedding
# approximates the geodesic distance between all pairs of vertices by
# computing the true geodesic distance to ``n_landmarks`` vertices
# that are randomly sampled.
# Higher values of ``n_landmarks`` lead to more precise embeddings, although
# they will take more time to compute. Note that this does not affect the
# speed of the rest of the alignment procedure, so you might want to invest
# computational time in deriving precise embeddings. Moreover, this step
# can easily be parallelized on CPUs.


(coordinates, triangles) = surface.load_surf_mesh(fsaverage5.pial_left)
fs5_pial_left_geometry_embeddings = lmds.compute_lmds(
    coordinates,
    triangles,
    n_landmarks=100,
    k=3,
    n_jobs=2,
    verbose=True,
)
source_geometry_embeddings = fs5_pial_left_geometry_embeddings
target_geometry_embeddings = fs5_pial_left_geometry_embeddings
source_geometry_embeddings.shape

# %%
# Each line ``vertex_index`` of the geometry matrices contains the anatomical
# distance (here in millimeters) from ``vertex_index`` to all other vertices
# of the mesh.

vertex_index = 12

fig = plt.figure(figsize=(5, 5))
ax = fig.add_subplot(111, projection="3d")
ax.set_title("Approximated geodesic distance in mm\non the cortical surface")
plot_surface_map(
    np.linalg.norm(
        source_geometry_embeddings
        - source_geometry_embeddings[vertex_index, :],
        axis=1,
    ),
    cmap="magma",
    cbar_tick_format="%.2f",
    axes=ax,
)
plt.show()

# %%
# Normalizing features and geometries
# -----------------------------------
# Features and embeddings should be normalized before we can train a mapping.
# Normalizing embeddings can be tricky, so we provide an empirical method
# to perform this operation: ``coarse_to_fine.random_normalizing()``
# samples pairs of indices in the embeddings vector and computes their
# L2 norm. Eventually, it divides the embeddings vector by the maximum
# norm it found in this process.

source_features_normalized = source_features / np.linalg.norm(
    source_features, axis=1
).reshape(-1, 1)
target_features_normalized = target_features / np.linalg.norm(
    target_features, axis=1
).reshape(-1, 1)

source_embeddings_normalized, source_distance_max = (
    coarse_to_fine.random_normalizing(source_geometry_embeddings)
)
target_embeddings_normalized, target_distance_max = (
    coarse_to_fine.random_normalizing(target_geometry_embeddings)
)

# %%
# Training the mapping
# --------------------
# Let's create our mappings. We will need 2 of them: one for computing
# an alignment between sub-samples of the source and target individuals,
# the other one for computing a fine-grained alignment leveraging
# information gathered during the coarse step.
# Note that the 2 solvers can use different parameters.

coarse_mapping = FUGW(alpha=0.5, rho=1, eps=1e-4)
coarse_mapping_solver = "mm"
coarse_mapping_solver_params = {
    "nits_bcd": 5,
    "tol_uot": 1e-10,
}

fine_mapping = FUGWSparse(alpha=0.5, rho=1, eps=1e-4)
fine_mapping_solver = "mm"
fine_mapping_solver_params = {
    "nits_bcd": 3,
    "tol_uot": 1e-10,
}

# %%
# Let's fit our mappings! Remember to use the training maps only.
# Moreover, note the importance of ``source_selection_radius`` and
# ``target_selection_radius``. Their meaning is the following:
# if a vertex ``i`` from the source was maximally matched with a vertex ``j``
# from the target during the coarse step, we allow vertices which are at most
# ``source_selection_radius`` away from ``i`` to be matched with vertices
# which are at most ``target_selection_radius`` away from ``j``.
# In this example, we set this radius to 10 millimeters,
# but **note that the radius
# should be normalized by the same coefficient that was used
# to normalize the respective embeddings matrices**.
# Finally, in this example, we sample 1000 vertices in the source and target
# individuals to compute our coarse mapping.

t0 = time.time()

source_sample, target_sample = coarse_to_fine.fit(
    # Source and target's features and embeddings
    source_features=source_features_normalized[:n_training_contrasts, :],
    target_features=target_features_normalized[:n_training_contrasts, :],
    source_geometry_embeddings=source_embeddings_normalized,
    target_geometry_embeddings=target_embeddings_normalized,
    # Parametrize step 1 (coarse alignment between source and target)
    source_sample_size=1000,
    target_sample_size=1000,
    coarse_mapping=coarse_mapping,
    coarse_mapping_solver=coarse_mapping_solver,
    coarse_mapping_solver_params=coarse_mapping_solver_params,
    # Parametrize step 2 (selection of pairs of indices present in
    # fine-grained's sparsity mask)
    coarse_pairs_selection_method="topk",
    source_selection_radius=10 / source_distance_max,
    target_selection_radius=10 / target_distance_max,
    # Parametrize step 3 (fine-grained alignment)
    fine_mapping=fine_mapping,
    fine_mapping_solver=fine_mapping_solver,
    fine_mapping_solver_params=fine_mapping_solver_params,
    # Misc
    verbose=True,
)

t1 = time.time()

# %%
# Here is the evolution of the FUGW loss while traning of the coarse mapping,
# with and without the entropic term. As you can see, we most likely
# stopped fitting the coarse mapping too early, yet it is probably enough
# for this example.

fig, ax = plt.subplots(figsize=(4, 4))
ax.set_title("Coarse mapping's training loss")
ax.set_ylabel("Loss")
ax.set_xlabel("BCD step")
ax.plot(coarse_mapping.loss_steps, coarse_mapping.loss, label="FUGW loss")
ax.plot(
    coarse_mapping.loss_steps,
    coarse_mapping.loss_entropic,
    label="FUGW entropic loss",
)
ax.legend()
plt.show()

# %%
# Here is that of the fine-grained mapping:

fig, ax = plt.subplots(figsize=(4, 4))
ax.set_title("Fine mapping's training loss")
ax.set_ylabel("Loss")
ax.set_xlabel("BCD step")
ax.plot(fine_mapping.loss_steps, fine_mapping.loss, label="FUGW loss")
ax.plot(
    fine_mapping.loss_steps,
    fine_mapping.loss_entropic,
    label="FUGW entropic loss",
)
ax.legend()
plt.show()

# %%
# Note how few iterations are neede for the fine-grained model to converge
# compared to the coarse one, although the coarse model usually runs much
# faster (in total time) than the fine-grained one.
# It probably is a good strategy to invest computational time in deriving
# a precise coarse solution.
print(f"Total training time: {t1 - t0:.1f}s")

# %%
# One can also inspect the sampled vertices used to derive the coarse mapping.
# Note that a poor sampling of some cortical areas can have really bad
# consequences on the fine-grained mapping, in which case you should probably
# increase the number of samples (as opposed to increasing the radius).
source_sampled_surface = np.zeros(source_features.shape[1])
source_sampled_surface[source_sample] = 1
target_sampled_surface = np.zeros(target_features.shape[1])
target_sampled_surface[target_sample] = 1

fig = plt.figure(figsize=(3 * 2, 3))
fig.suptitle("Sampled vertices")
grid_spec = gridspec.GridSpec(1, 2, figure=fig)

ax = fig.add_subplot(grid_spec[0, 0], projection="3d")
ax.set_title("Source individual")
plot_surface_map(
    source_sampled_surface, cmap="Blues", avg_method="max", axes=ax
)

ax = fig.add_subplot(grid_spec[0, 1], projection="3d")
ax.set_title("Target individual")
plot_surface_map(
    target_sampled_surface, cmap="Blues", avg_method="max", axes=ax
)

plt.show()

# %%
# Using the computed mappings
# ---------------------------
# In this example, the transport plan of the coarse mapping
# is already too big to be displayed, but we can still look at the top-left
# corder.
# In should not show any meaningful structure because the indices on which
# it was computed were sampled at random.

coarse_pi = coarse_mapping.pi.numpy()
fig, ax = plt.subplots(figsize=(10, 10))
ax.set_title("Coarse transport plan (top-left corner)", fontsize=20)
ax.set_xlabel("target vertices", fontsize=15)
ax.set_ylabel("source vertices", fontsize=15)
im = plt.imshow(coarse_pi[:200, :200], cmap="viridis")
plt.colorbar(im, ax=ax, shrink=0.8)
plt.show()

# %%
# However, we can visualize the sparse transport plan computed by the
# fine-grained mapping, which is much more informative.
# Indeed, it exhibits some structure because the source and target meshes
# are the same: indeed, assuming vertex correspondance between the source
# and target mesh should already yield a reasonable alignment,
# we expected the diagonal of this matrix to be non-null.

indices = fine_mapping.pi.indices()
fine_mapping_as_scipy_coo = coo_matrix(
    (
        fine_mapping.pi.values(),
        (indices[0], indices[1]),
    ),
    shape=fine_mapping.pi.size(),
)

fig, ax = plt.subplots(figsize=(10, 10))
ax.set_title("Sparsity mask of fine-grained mapping", fontsize=15)
ax.set_ylabel("Source vertices", fontsize=15)
ax.set_xlabel("Target vertices", fontsize=15)
plt.spy(fine_mapping_as_scipy_coo, precision="present", markersize=0.01)
plt.show()

# %%
# In this example, the computed sparse transport plan is quite sparse:
# it stores about 0.5% of what the equivalent dense transport plan
# would store.

100 * fine_mapping.pi.values().shape[0] / fine_mapping.pi.shape.numel()


# %%
# Each line ``vertex_index`` of the computed mapping can be interpreted as
# a probability map describing which vertices of the target
# should be mapped with the source vertex ``vertex_index``.
# Since the ith row of a sparse matrix is not always easy to access,
# we fetch it by using ``.inverse_transform()`` on a one-hot vertor
# whose only non-null coefficient is at position ``vertex_index``.

one_hot = np.zeros(source_features.shape[1])
one_hot[vertex_index] = 1.0
probability_map = fine_mapping.inverse_transform(one_hot)


fig = plt.figure(figsize=(5, 5))
ax = fig.add_subplot(111, projection="3d")
ax.set_title(
    "Probability map of target vertices\n"
    f"being matched with source vertex {vertex_index}"
)
plot_surface_map(probability_map, cmap="viridis", axes=ax)
plt.show()

# %%
# Using ``mapping.transform()``,
# we can use the computed mapping to transport any collection of feature maps
# from the source anatomy onto the target anatomy.
# Note that, conversely, ``mapping.inverse_transform()`` takes feature maps
# from the target anatomy and transports them on the source anatomy.

contrast_index = 2
predicted_target_features = fine_mapping.transform(
    source_features[contrast_index, :]
)
predicted_target_features.shape

# %%

fig = plt.figure(figsize=(3 * 3, 3))
fig.suptitle("Transporting feature maps of the training set")
grid_spec = gridspec.GridSpec(1, 3, figure=fig)

ax = fig.add_subplot(grid_spec[0, 0], projection="3d")
ax.set_title("Actual source features")
plot_surface_map(
    source_features[contrast_index, :], axes=ax, vmax=10, vmin=-10
)

ax = fig.add_subplot(grid_spec[0, 1], projection="3d")
ax.set_title("Predicted target features")
plot_surface_map(predicted_target_features, axes=ax, vmax=10, vmin=-10)

ax = fig.add_subplot(grid_spec[0, 2], projection="3d")
ax.set_title("Actual target features")
plot_surface_map(
    target_features[contrast_index, :], axes=ax, vmax=10, vmin=-10
)

plt.show()

# %%
# Here, we transported a feature map which is part of the traning set,
# which does not really help evaluate the quality of our model.
# Instead, we can also use the computed mapping to transport unseen data,
# which is how we will usually assess whether our model has captured
# useful information or not:

contrast_index = len(contrasts) - 1
predicted_target_features = fine_mapping.transform(
    source_features[contrast_index, :]
)

fig = plt.figure(figsize=(3 * 3, 3))
fig.suptitle("Transporting feature maps of the test set")
grid_spec = gridspec.GridSpec(1, 3, figure=fig)

ax = fig.add_subplot(grid_spec[0, 0], projection="3d")
ax.set_title("Actual source features")
plot_surface_map(
    source_features[contrast_index, :], axes=ax, vmax=10, vmin=-10
)

ax = fig.add_subplot(grid_spec[0, 1], projection="3d")
ax.set_title("Predicted target features")
plot_surface_map(predicted_target_features, axes=ax, vmax=10, vmin=-10)

ax = fig.add_subplot(grid_spec[0, 2], projection="3d")
ax.set_title("Actual target features")
plot_surface_map(
    target_features[contrast_index, :], axes=ax, vmax=10, vmin=-10
)

plt.show()
