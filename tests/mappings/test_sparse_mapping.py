from itertools import product

import numpy as np
import pytest
import torch

from fugw.mappings import FUGWSparse
from fugw.utils import init_mock_distribution

np.random.seed(0)
torch.manual_seed(0)

n_voxels_source = 105
n_voxels_target = 95
n_features_train = 10
n_features_test = 5

return_numpys = [True, False]
sparse_layouts = ["coo", "csr"]

devices = [torch.device("cpu")]
if torch.cuda.is_available():
    devices.append(torch.device("cuda:0"))

solvers = ["sinkhorn", "mm", "ibpp"]


@pytest.mark.parametrize(
    "device,return_numpy,solver", product(devices, return_numpys, solvers)
)
def test_sparse_mapping(device, return_numpy, solver):
    # Generate random training data for source and target
    _, source_features_train, _, source_embeddings = init_mock_distribution(
        n_features_train, n_voxels_source, return_numpy=return_numpy
    )
    _, target_features_train, _, target_embeddings = init_mock_distribution(
        n_features_train, n_voxels_target, return_numpy=return_numpy
    )

    fugw = FUGWSparse()
    fugw.fit(
        source_features=source_features_train,
        target_features=target_features_train,
        source_geometry_embedding=source_embeddings,
        target_geometry_embedding=target_embeddings,
        solver=solver,
        solver_params={
            "nits_bcd": 3,
            "ibpp_eps_base": 1e8,
        },
        device=device,
    )

    # Use trained model to transport new features
    # 1. with numpy arrays
    source_features_test = np.random.rand(n_features_test, n_voxels_source)
    target_features_test = np.random.rand(n_features_test, n_voxels_target)
    source_features_on_target = fugw.transform(source_features_test)
    assert source_features_on_target.shape == target_features_test.shape
    assert isinstance(source_features_on_target, np.ndarray)
    target_features_on_source = fugw.inverse_transform(target_features_test)
    assert target_features_on_source.shape == source_features_test.shape
    assert isinstance(target_features_on_source, np.ndarray)

    source_features_test = np.random.rand(n_voxels_source)
    target_features_test = np.random.rand(n_voxels_target)
    source_features_on_target = fugw.transform(source_features_test)
    assert source_features_on_target.shape == target_features_test.shape
    assert isinstance(source_features_on_target, np.ndarray)
    target_features_on_source = fugw.inverse_transform(target_features_test)
    assert target_features_on_source.shape == source_features_test.shape
    assert isinstance(target_features_on_source, np.ndarray)

    # 2. with torch tensors
    source_features_test = torch.rand(n_features_test, n_voxels_source)
    target_features_test = torch.rand(n_features_test, n_voxels_target)
    source_features_on_target = fugw.transform(source_features_test)
    assert source_features_on_target.shape == target_features_test.shape
    assert isinstance(source_features_on_target, torch.Tensor)
    target_features_on_source = fugw.inverse_transform(target_features_test)
    assert target_features_on_source.shape == source_features_test.shape
    assert isinstance(target_features_on_source, torch.Tensor)

    source_features_test = torch.rand(n_voxels_source)
    target_features_test = torch.rand(n_voxels_target)
    source_features_on_target = fugw.transform(source_features_test)
    assert source_features_on_target.shape == target_features_test.shape
    assert isinstance(source_features_on_target, torch.Tensor)
    target_features_on_source = fugw.inverse_transform(target_features_test)
    assert target_features_on_source.shape == source_features_test.shape
    assert isinstance(target_features_on_source, torch.Tensor)


@pytest.mark.parametrize(
    "device,sparse_layout,return_numpy",
    product(devices, sparse_layouts, return_numpys),
)
def test_fugw_sparse_with_init(device, sparse_layout, return_numpy):
    # Generate random training data for source and target
    _, source_features_train, _, source_embeddings = init_mock_distribution(
        n_features_train, n_voxels_source, return_numpy=return_numpy
    )
    _, target_features_train, _, target_embeddings = init_mock_distribution(
        n_features_train, n_voxels_target, return_numpy=return_numpy
    )

    rows = []
    cols = []
    items_per_row = 3
    for i in range(n_voxels_source):
        for _ in range(items_per_row):
            rows.append(i)
        cols.extend(np.random.permutation(n_voxels_target)[:items_per_row])

    if sparse_layout == "coo":
        init_plan = torch.sparse_coo_tensor(
            np.array([rows, cols]),
            np.ones(len(rows)) / len(rows),
            size=(n_voxels_source, n_voxels_target),
        ).to(device)
    elif sparse_layout == "csr":
        init_plan = (
            torch.sparse_coo_tensor(
                np.array([rows, cols]),
                np.ones(len(rows)) / len(rows),
                size=(n_voxels_source, n_voxels_target),
            )
            .to(device)
            .to_sparse_csr()
        )

    fugw = FUGWSparse()
    fugw.fit(
        source_features=source_features_train,
        target_features=target_features_train,
        source_geometry_embedding=source_embeddings,
        target_geometry_embedding=target_embeddings,
        init_plan=init_plan,
        device=device,
    )

    # Use trained model to transport new features
    source_features_test = torch.rand(n_features_test, n_voxels_source)
    target_features_test = torch.rand(n_features_test, n_voxels_target)
    source_features_on_target = fugw.transform(source_features_test)
    assert source_features_on_target.shape == target_features_test.shape
    assert isinstance(source_features_on_target, torch.Tensor)
    target_features_on_source = fugw.inverse_transform(target_features_test)
    assert target_features_on_source.shape == source_features_test.shape
    assert isinstance(target_features_on_source, torch.Tensor)

    source_features_test = torch.rand(n_voxels_source)
    target_features_test = torch.rand(n_voxels_target)
    source_features_on_target = fugw.transform(source_features_test)
    assert source_features_on_target.shape == target_features_test.shape
    assert isinstance(source_features_on_target, torch.Tensor)
    target_features_on_source = fugw.inverse_transform(target_features_test)
    assert target_features_on_source.shape == source_features_test.shape
    assert isinstance(target_features_on_source, torch.Tensor)
