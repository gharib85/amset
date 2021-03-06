import logging
from abc import ABC, abstractmethod
from copy import deepcopy
from typing import Any, Dict, Tuple

import numpy as np

from amset.constants import bohr_to_cm, nm_to_bohr
from amset.core.data import AmsetData
from amset.log import log_list
from amset.scattering.elastic import calculate_inverse_screening_length_sq
from BoltzTraP2.units import Second

logger = logging.getLogger(__name__)


class AbstractBasicScattering(ABC):

    name: str
    required_properties: Tuple[str]

    def __init__(self, materials_properties: Dict[str, Any], amset_data: AmsetData):
        self.properties = {p: materials_properties[p] for p in self.required_properties}
        self.doping = amset_data.doping
        self.temperatures = amset_data.temperatures
        self.nbands = {s: len(amset_data.energies[s]) for s in amset_data.spins}
        self.spins = amset_data.spins

    @property
    @abstractmethod
    def rates(self):
        pass


class ConstantRelaxationTime(AbstractBasicScattering):

    name = "CRT"
    required_properties = ("constant_relaxation_time",)

    def __init__(self, materials_properties: Dict[str, Any], amset_data: AmsetData):
        super().__init__(materials_properties, amset_data)
        rate = 1 / self.properties["constant_relaxation_time"]
        shape = {
            s: amset_data.fermi_levels.shape + amset_data.energies[s].shape
            for s in self.spins
        }
        self._rates = {s: np.full(shape[s], rate) for s in self.spins}

    @property
    def rates(self):
        # need to return rates with shape (nspins, ndops, ntemps, nbands, nkpoints)
        return self._rates


class MeanFreePathScattering(AbstractBasicScattering):

    name = "MFP"
    required_properties = ("mean_free_path",)

    def __init__(self, materials_properties: Dict[str, Any], amset_data: AmsetData):
        super().__init__(materials_properties, amset_data)

        # convert mean free path from nm to bohr
        mfp = self.properties["mean_free_path"] * nm_to_bohr

        self._rates = {}
        ir_kpoints_idx = amset_data.ir_kpoints_idx
        for spin in self.spins:
            vvelocities = amset_data.velocities_product[spin][..., ir_kpoints_idx]
            v = np.sqrt(np.diagonal(vvelocities, axis1=1, axis2=2))
            v = np.linalg.norm(v, axis=2)
            v[v < 0.005] = 0.005  # handle very small velocities
            velocities = np.tile(v, (len(self.doping), len(self.temperatures), 1, 1))

            rates = velocities * Second / mfp
            self._rates[spin] = rates[..., amset_data.ir_to_full_kpoint_mapping]

    @property
    def rates(self):
        # need to return rates with shape (nspins, ndops, ntemps, nbands, nkpoints)
        return self._rates


class BrooksHerringScattering(AbstractBasicScattering):

    name = "IMP"
    required_properties = ("acceptor_charge", "donor_charge", "static_dielectric")

    def __init__(self, materials_properties: Dict[str, Any], amset_data: AmsetData):
        # this is similar to the full IMP scattering, except for the prefactor
        # which follows the simplified BH formula
        super().__init__(materials_properties, amset_data)
        logger.debug("Initializing IMP (Brooks–Herring) scattering")

        inverse_screening_length_sq = calculate_inverse_screening_length_sq(
            amset_data, self.properties["static_dielectric"]
        )
        impurity_concentration = np.zeros(amset_data.fermi_levels.shape)

        imp_info = []
        for n, t in np.ndindex(inverse_screening_length_sq.shape):
            n_conc = np.abs(amset_data.electron_conc[n, t])
            p_conc = np.abs(amset_data.hole_conc[n, t])

            impurity_concentration[n, t] = (
                n_conc * self.properties["donor_charge"] ** 2
                + p_conc * self.properties["acceptor_charge"] ** 2
            )
            imp_info.append(
                "{:3.2e} cm⁻³ & {} K: β² = {:4.3e} a₀⁻², Nᵢᵢ = {:4.3e} cm⁻³".format(
                    amset_data.doping[n] * (1 / bohr_to_cm) ** 3,
                    amset_data.temperatures[t],
                    inverse_screening_length_sq[n, t],
                    impurity_concentration[n, t] * (1 / bohr_to_cm) ** 3,
                )
            )

        logger.debug(
            "Inverse screening length (β) and impurity concentration " "(Nᵢᵢ):"
        )
        log_list(imp_info, level=logging.DEBUG)

        # normalized energies has shape (nspins, ndoping, ntemps, nbands, n_ir_kpoints)
        normalized_energies = get_normalized_energies(amset_data)

        # dos effective masses has shape (nspins, nbands, n_ir_kpoints)
        dos_effective_masses = get_dos_effective_masses(amset_data)

        # screening has shape (ndoping, nbands, 1, 1)
        screening = inverse_screening_length_sq[..., None, None]

        prefactor = (
            impurity_concentration
            * 8
            * np.pi
            * Second
            / self.properties["static_dielectric"] ** 2
        )

        ir_kpoints_idx = amset_data.ir_kpoints_idx
        ir_to_full_idx = amset_data.ir_to_full_kpoint_mapping
        self._rates = {}
        for spin in self.spins:
            masses = np.tile(
                dos_effective_masses[spin],
                (len(self.doping), len(self.temperatures), 1, 1),
            )
            energies = normalized_energies[spin]

            k_sq = 2 * masses * energies
            vvelocities = amset_data.velocities_product[spin][..., ir_kpoints_idx]
            v = np.sqrt(np.diagonal(vvelocities, axis1=1, axis2=2))
            v = np.linalg.norm(v, axis=2) / np.sqrt(3)
            v[v < 0.005] = 0.005

            velocities = np.tile(v, (len(self.doping), len(self.temperatures), 1, 1))
            c = np.tile(
                amset_data.c_factor[spin][:, ir_kpoints_idx],
                (len(self.doping), len(self.temperatures), 1, 1),
            )

            d_factor = (
                1
                + (2 * screening * c ** 2 / k_sq)
                + (3 * screening ** 2 * c ** 4 / (4 * k_sq ** 2))
            )
            b_factor = (
                (4 * k_sq / screening) / (1 + 4 * k_sq / screening)
                + (8 * c ** 2 * (screening + 2 * k_sq) / (screening + 4 * k_sq))
                + (
                    c ** 4
                    * (3 * screening ** 2 + 6 * screening * k_sq - 8 * k_sq ** 2)
                    / ((screening + 4 * k_sq) * k_sq)
                )
            )

            b = (4 * k_sq) / screening

            self._rates[spin] = (
                prefactor[:, :, None, None]
                * (d_factor * np.log(1 + b) - b_factor)
                / (velocities * k_sq)
            )

            # these will be interpolated
            self._rates[spin][np.isnan(self.rates[spin])] = 0
            self.rates[spin][energies < 1e-8] = 0
            self.rates[spin] = self.rates[spin][..., ir_to_full_idx]

    @property
    def rates(self):
        # need to return rates with shape (nspins, ndops, ntemps, nbands, nkpoints)
        return self._rates


def get_normalized_energies(amset_data: AmsetData):
    # normalize the energies; energies returned as abs(E-Ef) (+ kBT if broaden is True)
    spins = amset_data.spins
    fermi_shape = amset_data.fermi_levels.shape
    ir_kpoints_idx = amset_data.ir_kpoints_idx
    energies = {}
    for spin in spins:
        energies[spin] = np.empty(
            fermi_shape + (len(amset_data.energies[spin]), len(ir_kpoints_idx))
        )

    if amset_data.is_metal:
        for spin in spins:
            spin_energies = deepcopy(amset_data.energies[spin][:, ir_kpoints_idx])
            for n, t in np.ndindex(fermi_shape):
                energies[spin][n, t] = np.abs(
                    spin_energies - amset_data.intrinsic_fermi_level
                )
    else:
        vb_idx = amset_data.vb_idx
        vbm_e = np.max([np.max(amset_data.energies[s][: vb_idx[s] + 1]) for s in spins])
        cbm_e = np.min([np.min(amset_data.energies[s][vb_idx[s] + 1 :]) for s in spins])
        for spin in spins:
            spin_energies = deepcopy(amset_data.energies[spin][:, ir_kpoints_idx])
            spin_energies[: vb_idx[spin] + 1] = (
                vbm_e - spin_energies[: vb_idx[spin] + 1]
            )
            spin_energies[vb_idx[spin] + 1 :] = (
                spin_energies[vb_idx[spin] + 1 :] - cbm_e
            )

            for n, t in np.ndindex(fermi_shape):
                energies[spin][n, t] = spin_energies

    return energies


def get_dos_effective_masses(amset_data: AmsetData):
    dos_effective_masses = {}
    for spin in amset_data.spins:
        spin_masses = amset_data.effective_mass[spin][..., amset_data.ir_kpoints_idx]
        masses_eig = np.diagonal(spin_masses, axis1=1, axis2=2)
        masses_abs = np.abs(masses_eig)
        dos_effective_masses[spin] = np.power(np.product(masses_abs, axis=2), 1 / 3)

    return dos_effective_masses
