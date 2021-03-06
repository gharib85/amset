import itertools

import h5py
import numpy as np
from monty.dev import requires
from tqdm.auto import tqdm

from amset.constants import numeric_types
from pymatgen import Spin, Structure
from pymatgen.io.vasp import Potcar, Vasprun

try:
    import pawpyseed as pawpy
except ImportError:
    pawpy = None


str_to_spin = {"up": Spin.up, "down": Spin.down}
spin_to_int = {Spin.up: 0, Spin.down: 1}
int_to_spin = {0: Spin.up, 1: Spin.down}

pawpy_msg = (
    "Pawpyseed is required for extracting wavefunction coefficients\nFollow the"
    "installation instructions at https://github.com/kylebystrom/pawpyseed"
)


@requires(pawpy, pawpy_msg)
def get_wavefunction(
    potcar="POTCAR",
    wavecar="WAVECAR",
    vasprun="vasprun.xml",
    directory=None,
    symprec=None,
):
    from pawpyseed.core.wavefunction import Wavefunction, CoreRegion
    from pawpyseed.core import pawpyc

    if directory:
        wf = Wavefunction.from_directory(path=directory)
    else:
        if isinstance(vasprun, str):
            vasprun = Vasprun(vasprun)

        if isinstance(potcar, str):
            potcar = Potcar.from_file(potcar)

        ngx = vasprun.parameters["NGX"]
        ngy = vasprun.parameters["NGY"]
        ngz = vasprun.parameters["NGZ"]
        dim = np.array([ngx, ngy, ngz])
        symprec = vasprun.parameters["SYMPREC"]
        structure = vasprun.final_structure

        pwf = pawpyc.PWFPointer(wavecar, vasprun)
        core_region = CoreRegion(potcar)

        wf = Wavefunction(structure, pwf, core_region, dim, symprec, False)

    dwf = wf.desymmetrized_copy(time_reversal_symmetry=False, symprec=symprec)
    return dwf


@requires(pawpy, pawpy_msg)
def get_wavefunction_coefficients(wavefunction, bs, iband=None, encut=600, pbar=True):
    from pawpyseed.core.momentum import MomentumMatrix

    mm = MomentumMatrix(wavefunction, encut=encut)
    if not iband:
        iband = {}

    coeffs = {}
    for spin_idx in range(wavefunction.nspin):
        spin = int_to_spin[spin_idx]
        spin_iband = iband.get(spin, None)

        coeffs[spin] = _get_spin_wavefunction_coefficients(
            mm, bs, spin, iband=spin_iband, pbar=pbar
        )
    return coeffs


def _get_spin_wavefunction_coefficients(mm, bs, spin, iband=None, pbar=True):
    from amset.constants import output_width

    if iband is None:
        iband = np.arange(bs.bands[spin].shape[0], dtype=int)

    elif isinstance(iband, numeric_types):
        iband = [iband]

    ncoeffs = mm.momentum_grid.shape[0]
    nkpoints = mm.wf.kpts.shape[0]
    ns = spin_to_int[spin]
    coeffs = np.zeros((len(iband), nkpoints, ncoeffs), dtype=complex)

    state_idxs = list(itertools.product(enumerate(iband), range(nkpoints)))
    if pbar:
        state_idxs = tqdm(state_idxs, ncols=output_width)

    for (i, nb), nk in state_idxs:
        coeffs[i, nk] = mm.get_reciprocal_fullfw(nb, nk, ns)
        coeffs[i, nk] /= np.linalg.norm(coeffs[i, nk])

    return coeffs


def dump_coefficients(coeffs, kpoints, structure, filename="coeffs.h5"):
    with h5py.File(filename, "w") as f:
        for spin, spin_coeffs in coeffs.items():
            name = "coefficients_{}".format(spin.name)
            f[name] = spin_coeffs

        f["structure"] = np.string_(structure.to_json())
        f["kpoints"] = kpoints


def load_coefficients(filename):
    coeffs = {}
    with h5py.File(filename, "r") as f:
        coeff_keys = [k for k in list(f.keys()) if "coefficients" in k]
        for key in coeff_keys:
            spin = str_to_spin[key.split("_")[1]]
            coeffs[spin] = np.array(f[key])

        structure_str = np.string_(np.array(f["structure"])).decode()
        structure = Structure.from_str(structure_str, fmt="json")
        kpoints = np.array(f["kpoints"])

    return coeffs, kpoints, structure
