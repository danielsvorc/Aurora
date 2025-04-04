r"""
Aurora functionality to set up and run KN1D to extract atomic and neutral 
background densities at the edge. 

KN1D is a 1D kinetic neutral code originally developed by B.LaBombard (MIT). 
For information, refer to the `KN1D manual  <https://github.com/fsciortino/kn1d/blob/master/kn1d_manual.pdf>`__ .

Note that this Aurora module is merely a wrapper of KN1D. Users require an
IDL license on the computer where this module is called in order to be able to 
run KN1D. The IDL (and Fortran) code themselves are automatically downloaded and
compiled by this module.
"""
# MIT License
#
# Copyright (c) 2021 Francesco Sciortino
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

from scipy.interpolate import interp1d
import numpy as np
import os
import scipy.io
from scipy.integrate import cumtrapz
import matplotlib.pyplot as plt
from scipy.constants import e, h, c as c_light, Rydberg
import copy

import time

from . import neutrals
from . import coords

import pleque # type: ignore
from omfit_classes import omfit_eqdsk

import subprocess

thisdir = os.path.dirname(os.path.realpath(__file__))

'''
CUSTOM FUNCTIONS
'''

def pa_to_mTorr(p):
    return p/133.32*1000

def mTorr_to_pa(p):
    return p/1000*133.32

def output_postprocessing():
    return

def exp_decay(q0, lambda_q, x):
    return q0*np.exp(-x/lambda_q)

def _setup_kin_profs_x(
    x,
    ne_m3_in,
    Te_eV_in,
    Ti_eV_in,
    sep_m,
    lim_m,
    wall_m,
    kin_prof_exp_decay_SOL=False,
    kin_prof_exp_decay_LS=False,
    ne_decay_len_m=[0.01, 0.01, 0.01],
    Te_decay_len_m=[0.01, 0.01, 0.01],
    Ti_decay_len_m=[0.01, 0.01, 0.01],
    near_far_SOL_boundary_m = 0.0,
    ne_min_m3=1e12,
    Te_min_eV=0.1,
    Ti_min_eV=0.1
):
    """Private method to set up kinetic profiles to the format required by
    :py:func:`~aurora.kn1d.run_kn1d`. Refer to this function for descriptions of inputs.

    This function returns ne, Te and Ti profiles on the x_to_wall_m radial grid,
    from the core to the wall.

    Parameters
    ----------
    x : 1D array
        Coordinates on which ne_cm3, Te_eV and Ti_eV are given. (Like R,Z)
    ne_m3_in : 1D array
        Electron density on rhop grid [:math:`cm^{-3}`].
    Te_eV_in : 1D array
        Electron temperature on rhop grid [:math:`eV`].
    Ti_eV_in : 1D array
        Main ion temperature on rhop grid [:math:`eV`].
    sep_m : float
        separatrix coordinates [m]
    lim_m : float
        limiter coordinates [m]
    kin_prof_exp_decay_SOL : bool
        If True, kinetic profiles are set to exponentially decay over the SOL region.
    kin_prof_exp_decay_LS : bool
        If True, kinetic profiles are set to exponentially decay over the LS region.
    ne_decay_len_m : list of 2 float
        Exponential decay lengths of electron density in the near SOL, far SOL, and LS regions.
        Default is [1,1,1] :math:`m`.
    Te_decay_len_cm : float
        Exponential decay lengths of electron temperature in the near SOL, far SOL, and LS regions.
        Default is [1,1,1] :math:`m`.
    Ti_decay_len_cm : float
        Exponential decay lengths of main ion temperature in the near SOL, far SOL, and LS regions.
        Default is [1,1,1] :math:`m`.
    ne_min_m3 : float
        Minimum electron density across profile. Default is :math:`10^{12} m^{-3}`.
    Te_min_eV : float
        Minimum electron temperaure across profile. Default is :math:`eV`.
    Ti_min_eV : float
        Minimum main ion temperaure across profile. Default is :math:`eV`.

    Returns
    -------
    x_to_wall_m : 1D array
        Midradius coordinate from the magnetic axis to the wall. Units of [:math:`m`].
    ne_m3 : 1D array
        Electron density [:math:`m^{-3}`] on the x_to_wall_m grid.
    Te_eV : 1D array
        Electron temperature [:math:`eV`] on the x_to_wall_m grid.
    Ti_eV : 1D array
        Main ion temperature [:math:`eV`] on the x_to_wall_m grid.
    """

    # convert radial coordinate to rmid
    #rmid = coords.rad_coord_transform(rhop, "rhop", "rmid", geqdsk)  # m

    # define radial regions in the SOL in coordinates centered on the mag axis
    #rsep = coords.rad_coord_transform(1.0, "rhop", "rmid", geqdsk)
    #rwall = rsep + bound_sep_cm * 1e-2  # cm-->m
    #rlim = rsep + lim_sep_cm * 1e-2  # cm-->m

    # interpolate profiles on grid extending to wall
    x_to_wall_m = np.linspace(np.min(x), wall_m, 1000)  # 101) #201)
    ne_m30 = interp1d(x, ne_m3_in, bounds_error=False)(x_to_wall_m) 
    Te_eV0 = interp1d(x, Te_eV_in, bounds_error=False)(x_to_wall_m)
    Ti_eV0 = interp1d(x, Ti_eV_in, bounds_error=False)(x_to_wall_m)
    ne_m3 = np.ones_like(ne_m30)
    Te_eV = np.ones_like(Te_eV0)
    Ti_eV = np.ones_like(Ti_eV0)

    indLCFS = np.searchsorted(x_to_wall_m, sep_m)
    ind_far_SOL = np.searchsorted(x_to_wall_m, near_far_SOL_boundary_m)
    indLS = np.searchsorted(x_to_wall_m, lim_m)
    ind_end = np.searchsorted(x_to_wall_m, x[-1])

    # if kinetic profiles don't extend far enough in radius, we must set an exp decay depending on the radial region
    if ind_end < ind_far_SOL:
        # decays in the near SOL (to the wall)
        ne_m3_near_sol = exp_decay(ne_m30[ind_end - 1], ne_decay_len_m[0], x_to_wall_m[ind_end:ind_far_SOL]-x_to_wall_m[ind_end-1])
        Te_eV_near_sol = exp_decay(Te_eV0[ind_end - 1], Te_decay_len_m[0], x_to_wall_m[ind_end:ind_far_SOL]-x_to_wall_m[ind_end-1])
        Ti_eV_near_sol = exp_decay(Ti_eV0[ind_end - 1], Ti_decay_len_m[0], x_to_wall_m[ind_end:ind_far_SOL]-x_to_wall_m[ind_end-1])

        ne_m31 = np.concatenate((ne_m30[:ind_end], ne_m3_near_sol))
        Te_eV1 = np.concatenate((Te_eV0[:ind_end], Te_eV_near_sol))
        Ti_eV1 = np.concatenate((Ti_eV0[:ind_end], Ti_eV_near_sol))
    else:
        ne_m31 = copy.deepcopy(ne_m30)
        Te_eV1 = copy.deepcopy(Te_eV0)
        Ti_eV1 = copy.deepcopy(Ti_eV0)

    if ind_end < indLS:
        # decays in the far SOL (to the wall)
        ind = np.max(ind_end, ind_far_SOL)
        ne_m3_far_sol = exp_decay(ne_m31[ind - 1], ne_decay_len_m[1], x_to_wall_m[ind:indLS]-x_to_wall_m[ind-1])
        Te_eV_far_sol = exp_decay(Te_eV1[ind - 1], Te_decay_len_m[1], x_to_wall_m[ind:indLS]-x_to_wall_m[ind-1])
        Ti_eV_far_sol = exp_decay(Ti_eV1[ind - 1], Ti_decay_len_m[1], x_to_wall_m[ind:indLS]-x_to_wall_m[ind-1])

        ne_m32 = np.concatenate((ne_m31[:ind], ne_m3_far_sol))
        Te_eV2 = np.concatenate((Te_eV1[:ind], Te_eV_far_sol))
        Ti_eV2 = np.concatenate((Ti_eV1[:ind], Ti_eV_far_sol))
    else:
        ne_m32 = copy.deepcopy(ne_m31)
        Te_eV2 = copy.deepcopy(Te_eV1)
        Ti_eV2 = copy.deepcopy(Ti_eV1)
    
    if ind_end < len(x_to_wall_m):
        # decays in the LS (to the wall)
        ind = np.max([ind_end, indLS])
        ne_m3_LS = exp_decay(ne_m32[ind - 1], ne_decay_len_m[2], x_to_wall_m[ind:]-x_to_wall_m[ind-1])
        Te_eV_LS = exp_decay(Te_eV2[ind - 1], Te_decay_len_m[2], x_to_wall_m[ind:]-x_to_wall_m[ind-1])
        Ti_eV_LS = exp_decay(Ti_eV2[ind - 1], Ti_decay_len_m[2], x_to_wall_m[ind:]-x_to_wall_m[ind-1])

        ne_m3 = np.concatenate((ne_m32[:ind], ne_m3_LS))
        Te_eV = np.concatenate((Te_eV2[:ind], Te_eV_LS))
        Ti_eV = np.concatenate((Ti_eV2[:ind], Ti_eV_LS))
    else:
        ne_m3 = copy.deepcopy(ne_m32)
        Te_eV = copy.deepcopy(Te_eV2)
        Ti_eV = copy.deepcopy(Ti_eV2)


    # User may want to set exp decay in SOL or LS in place of dubious experimental data
    if kin_prof_exp_decay_SOL:
        # decays in the SOL
        ne_m3[indLCFS:ind_far_SOL] = exp_decay(ne_m3[indLCFS-1], ne_decay_len_m[0], x_to_wall_m[indLCFS:ind_far_SOL] - x_to_wall_m[indLCFS - 1])
        Te_eV[indLCFS:ind_far_SOL] = exp_decay(Te_eV[indLCFS-1], Te_decay_len_m[0], x_to_wall_m[indLCFS:ind_far_SOL] - x_to_wall_m[indLCFS - 1])
        Ti_eV[indLCFS:ind_far_SOL] = exp_decay(Ti_eV[indLCFS-1], Ti_decay_len_m[0], x_to_wall_m[indLCFS:ind_far_SOL] - x_to_wall_m[indLCFS - 1])

        ne_m3[ind_far_SOL:indLS] = exp_decay(ne_m3[ind_far_SOL-1], ne_decay_len_m[1], x_to_wall_m[ind_far_SOL:indLS] - x_to_wall_m[ind_far_SOL - 1])
        Te_eV[ind_far_SOL:indLS] = exp_decay(Te_eV[ind_far_SOL-1], Te_decay_len_m[1], x_to_wall_m[ind_far_SOL:indLS] - x_to_wall_m[ind_far_SOL - 1])
        Ti_eV[ind_far_SOL:indLS] = exp_decay(Ti_eV[ind_far_SOL-1], Ti_decay_len_m[1], x_to_wall_m[ind_far_SOL:indLS] - x_to_wall_m[ind_far_SOL - 1])

    if kin_prof_exp_decay_LS:
        ne_m3[indLS:] = exp_decay(ne_m3[indLS-1], ne_decay_len_m[2], x_to_wall_m[indLS:] - x_to_wall_m[indLS - 1])
        Te_eV[indLS:] = exp_decay(Te_eV[indLS-1], Te_decay_len_m[2], x_to_wall_m[indLS:] - x_to_wall_m[indLS - 1])
        Ti_eV[indLS:] = exp_decay(Ti_eV[indLS-1], Ti_decay_len_m[2], x_to_wall_m[indLS:] - x_to_wall_m[indLS - 1])

    # set minima across radial profiles
    ne_m3[ne_m3 < ne_min_m3] = ne_min_m3
    Te_eV[Te_eV < Te_min_eV] = Te_min_eV
    Ti_eV[Ti_eV < Ti_min_eV] = Ti_min_eV

    return x_to_wall_m, ne_m3, Te_eV, Ti_eV

def run_kn1d_x(
    x,
    ne_m3,
    Te_eV,
    Ti_eV,
    p_H2_Pa,
    clen_divertor_m,
    clen_limiter_m,
    sep_m,
    lim_m,
    wall_m,
    psi_n=None,
    innermost_x_m=1.0,
    mu=2.0,
    pipe_diag_m=0.0,
    vx=0.0,
    collisions={},
    kin_prof_exp_decay_SOL=False,
    kin_prof_exp_decay_LS=False,
    ne_decay_len_m=[0.01, 0.01, 0.01],
    Te_decay_len_m=[0.01, 0.01, 0.01],
    Ti_decay_len_m=[0.01, 0.01, 0.01],
    near_far_SOL_boundary_m = 0.0,
    ne_min_m3=1e6,
    Te_min_eV=0.01,
    Ti_min_eV=0.01,
    plot_kin_profs=False,
    KN1D_path='/compass/Shared/Common/IT/projects/kn1d-custom/',
    truncate=1e-3
):
    """Run KN1D for the given parameters. Refer to the KN1D manual for details.

    Depending on the provided options, kinetic profiles are extended beyond the Last Closed
    Flux Surface (LCxFS) and the Limiter Shadow (LS) via exponential decays with specified
    decay lengths. It is assumed that the given kinetic profiles extend from the core until
    at least the LCFS. All inputs are taken to be time-independent.

    This function automatically checks if a KN1D repository is available; if it is not,
    it obtains it from the web and compiles the necessary code.

    Note that an IDL license must be available. Aurora does not currently include a Python
    translation of KN1D -- it only acts as a wrapper.

    Parameters
    ----------
    x : 1D array
        x
    ne_m3 : 1D array
        Electron density on rhop grid [:math:`cm^{-3}`].
    Te_eV : 1D array
        Electron temperature on rhop grid [:math:`eV`].
    Ti_eV : 1D array
        Main ion temperature on rhop grid [:math:`eV`].
    eq : equilibrium object from pleque
    p_H2_mTorr : float
        Pressure of molecular hydrogen-isotopes measured at the wall. This may be estimated
        from experimental pressure gauges. This variable effectively sets the amplitude of the
        neutral source at the edge. Units of :math:`mTorr`.
    clen_divertor_m : float
        Connection length from the midplane to the divertor [:math:`m`].
    clen_limiter_m : float
        Connection length from the midplane to the limiter [:math:`m`].
    sep_m : float
        Absolute position of the separatrix on the x input grid.
    lim_m : float
        Absolute position of the limiter on the x input grid.
    wall_m :
        Absolute position of the wall on the x input grid.
    innermost_x_m : float
        Absolute position to of the innermost boundary to solve for.
    mu : float
        Atomic mass number of simulated species. Default is 2.0 (D).
    pipe_diag_m : float
        Diameter of the pipe through which H2 pressure is measured (see `p_H2_mTorr` variable).
        If left to 0, this diameter is effectively set to infinity. Default is 0.
    vx : float
        Radial velocity imposed on neutrals. This only has a weak effect usually.
        Default is 0 [:math:`cm/s`].
    collisions : dict
        Collision terms flags. Set each to True or False. If any of the flags are not given,
        all collision terms are internally set to be active. Possible flags are
        'H2_H2_EL','H2_P_EL','H2_H_EL','H2_HP_CX','H_H_EL','H_P_CX','H_P_EL','Simple_CX'
    kin_prof_exp_decay_SOL : bool
        If True, kinetic profiles are set to exponentially decay over the SOL region.
    kin_prof_exp_decay_LS : bool
        If True, kinetic profiles are set to exponentially decay over the LS region.
    ne_decay_len_m : list of 2 float
        Exponential decay lengths of electron density in the near SOL, far SOL, and LS regions.
        Default is [1,1] :math:`m`.
    Te_decay_len_m : float
        Exponential decay lengths of electron temperature in the near SOL, far SOL, and LS regions.
        Default is [1,1] :math:`m`.
    Ti_decay_len_m : float
        Exponential decay lengths of main ion temperature in the near SOL, far SOL, and LS regions.
        Default is [1,1] :math:`m`.
    near_far_SOL_boundary_m : float
        Absolute position of the boundary between the near and far SOL
    ne_min_m3 : float
        Minimum electron density across profile. Default is :math:`10^{12} m^{-3}`.
    Te_min_eV : float
        Minimum electron temperaure across profile. Default is :math:`eV`.
    Ti_min_eV : float
        Minimum main ion temperaure across profile. Default is :math:`eV`.
    plot_kin_profs : bool
        If True, kinetic profiles input to KN1D are plotted.

    Returns
    -------
    dict
        KN1D results and inputs, all collected into a dictionary. See example script for
        an illustration of using this.

    Notes
    -----
    For an example application, see the examples/aurora_kn1d.py script.
    """
    #convert p_H2_Pa to mTorr to input to KN1D
    p_H2_mTorr = pa_to_mTorr(p_H2_Pa)
    if KN1D_path != None:
        os.environ['IDL_PATH']=KN1D_path
    t = time.time()
    if "IDL_STARTUP" not in os.environ and "IDL_HOME" not in os.environ:
        raise ValueError(
            "An IDL installation does not seem to be available! KN1D cannot be run."
        )

    cwd = os.getcwd()

    # make sure that the KN1D source code is accessible.
    if "KN1D" not in os.listdir(thisdir):
        # if 'KN1D_DIR' not in os.environ:
        # git clone the KN1D repository
        #os.system(f"git clone https://repo.tok.ipp.cas.cz/svorc/kn1d-custom {thisdir}/KN1D")

        #os.chdir(f"{thisdir}/KN1D")
        # compile fortran libraries
        #print(f"export KN1D_DIR={thisdir}/KN1D; make clean; make")
        pass
        #os.system(f"export KN1D_DIR={thisdir}/KN1D; make clean; make")
        #os.chdir(cwd)
        # else:
        # copy KN1D directory locally
        # shutil.copytree(os.environ['KN1D_DIR'],thisdir+'/KN1D')
    else:
        # KN1D directory already available, assumed to be already built
        # NB: users need to have write-access to this directory!
        pass

    kn1d = {}
    kn1d.update(collisions)
    if "H2_H2_EL" not in kn1d:
        kn1d["H2_H2_EL"] = True
    if "H2_P_EL" not in kn1d:
        kn1d["H2_P_EL"] = True
    if "H2_H_EL" not in kn1d:
        kn1d["H2_H_EL"] = True
    if "H2_HP_CX" not in kn1d:
        kn1d["H2_HP_CX"] = True
    if "H_H_EL" not in kn1d:
        kn1d["H_H_EL"] = True
    if "H_P_CX" not in kn1d:
        kn1d["H_P_CX"] = True
    if "H_P_EL" not in kn1d:
        kn1d["H_P_EL"] = True
    if "Simple_CX" not in kn1d:
        kn1d["Simple_CX"] = False

    # get kinetic profiles on rmid_to_wall_m grid, applying exponential decays in SOL as requested
    x_to_wall_m, _ne_m3, _Te_eV, _Ti_eV = _setup_kin_profs_x(
        x,
        ne_m3,
        Te_eV,
        Ti_eV,
        sep_m,
        lim_m,
        wall_m,
        kin_prof_exp_decay_SOL,
        kin_prof_exp_decay_LS,
        ne_decay_len_m,
        Te_decay_len_m,
        Ti_decay_len_m,
        near_far_SOL_boundary_m,
        ne_min_m3,
        Te_min_eV,
        Ti_min_eV,
    )

    if plot_kin_profs:
        # show kinetic profiles going into KN1D modeling
        plot_input_kin_prof_x(
            x_to_wall_m,
            _ne_m3,
            _Te_eV,
            _Ti_eV,
            innermost_x_m,
            sep_m,
            lim_m,
        )

    #rhop = coords.rad_coord_transform(x_to_wall_m, "rmid", "rhop", geqdsk)
    #rwall_m = x_to_wall_m[-1]  # m
    #rsep_m = coords.rad_coord_transform(1.0, "rhop", "rmid", geqdsk)  # m
    #rlim_m = rsep_m + lim_sep_cm * 1e-2  # m

    # KN1D defines coordinates from the wall INWARD. Invert now:
    #r_kn1d_m = np.abs(x_to_wall_m - rwall_m)[::-1]
    x_kn1d = np.abs(x_to_wall_m-wall_m)[::-1]
    #rlim_kn1d_m = np.abs(rlim_m - rwall_m)
    lim_kn1d = np.abs(lim_m-wall_m)
    #rsep_kn1d_m = np.abs(rsep_m - rwall_m)
    sep_kn1d = np.abs(sep_m-wall_m)

    # diameter of pressure gauge pipe. Allows collisions with side-walls to be simulated
    dPipe = (
        pipe_diag_m * np.ones(len(x_to_wall_m))
    )  # m  -- zero values are treated as infinity

    # define the connection length vector
    lc = np.zeros(len(x_to_wall_m))
    lc[(sep_m < x_to_wall_m) * (x_to_wall_m < lim_m)] = (
        clen_divertor_m
    )  # m
    lc[x_to_wall_m > lim_m] = clen_limiter_m  # m

    def idl_array(arr, num):
        # set arrays into string form for IDL format
        return "[" + ",".join([str(round(val, num)) for val in arr]) + "]"

    def round_arr(arr, num, dtype=str):
        # avoid issues with floating-point precision
        return np.array([round(val, num) for val in arr], dtype=dtype)

    # cut all radial profiles to given innermost location
    ind_in = np.searchsorted(
        x_kn1d, innermost_x_m
    )  # r_kn1d_m is from wall inwards

    # Save arrays in final forms for KN1D
    num = 5  # digital point precision
    kn1d["x"] = round_arr(x_kn1d[:ind_in], num, float)
    ne_ = _ne_m3[::-1]
    kn1d["ne_m3"] = round_arr(ne_[:ind_in], num, float)  # m^-3
    Te_ = _Te_eV[::-1]
    kn1d["Te_eV"] = round_arr(Te_[:ind_in], num, float)  # eV
    Ti_ = _Ti_eV[::-1]
    kn1d["Ti_eV"] = round_arr(Ti_[:ind_in], num, float)  # eV
    dPipe_ = dPipe[::-1]
    kn1d["dPipe"] = round_arr(dPipe_[:ind_in], num, float)
    lc_ = lc[::-1]
    kn1d["lc"] = round_arr(lc_[:ind_in], num, float)
    kn1d["xlim"] = lim_kn1d
    kn1d["xsep"] = sep_kn1d
    kn1d["p_H2_mTorr"] = p_H2_mTorr  # mTorr
    kn1d["mu"] = mu

    # finally, set plasma radial velocity profile (negative is towards the wall [m s^-1])
    vx = np.ones_like(kn1d["x"]) * vx
    kn1d["vx"] = round_arr(vx, num, float)

    # Create IDL script to run KN1D
    # KN1D_code_path=os.getenv("IDL_PATH")+"/"
    print("KN1D_code_path=",KN1D_path)
    print("Creating IDL script to run KN1D...")
    idl_vars = {
        "x" : idl_array(kn1d["x"], num),
        "x_lim" : kn1d["xlim"],
        "xsep" : kn1d["xsep"],
        "gaugeH2" : kn1d["p_H2_mTorr"],
        "mu" : kn1d["mu"],
        "Ti" : idl_array(kn1d["Ti_eV"], num),
        "Te" : idl_array(kn1d["Te_eV"], num),
        "ne" : idl_array(kn1d["ne_m3"], num),
        "vx" : idl_array(kn1d["vx"], num),
        "lc" : idl_array(kn1d["lc"], num),
        "dPipe" : idl_array(kn1d["dPipe"], num),
        "H2_H2_EL" : int(kn1d["H2_H2_EL"]),
        "H2_P_EL" : int(kn1d["H2_P_EL"]),
        "H2_H_EL" : int(kn1d["H2_H_EL"]),
        "H_H_EL" : int(kn1d["H_H_EL"]),
        "H_P_CX" : int(kn1d["H_P_CX"]),
        "H_P_EL" : int(kn1d["H_P_EL"]),
        "Simple_CX" : int(kn1d["Simple_CX"]),
        "Outdir" : cwd,
        "KN1D_path" : KN1D_path,
        "truncate" : truncate,
    }
    idl_cmd = f"""
    ; Input data for KN1D run
    x = {idl_vars["x"]}
    x_lim = {idl_vars["x_lim"]:.5f}
    xsep = {idl_vars["xsep"]:.5f}
    gaugeH2 = {idl_vars["gaugeH2"]:}
    mu = {idl_vars["mu"]:}
    Ti = {idl_vars["Ti"]:}
    Te = {idl_vars["Te"]:}
    dens = {idl_vars["ne"]:}
    vx = {idl_vars["vx"]:}
    lc = {idl_vars["lc"]:}
    dPipe = {idl_vars["dPipe"]:}

    ; collisions options are set via a common block
    common KN1D_collisions,H2_H2_EL,H2_P_EL,H2_H_EL,H2_HP_CX,H_H_EL,H_P_EL,H_P_CX,Simple_CX

    H2_H2_EL= {idl_vars["H2_H2_EL"]:}
    H2_P_EL = {idl_vars["H2_P_EL"]:}
    H2_H_EL = {idl_vars["H2_H_EL"]:}
    H_H_EL= {idl_vars["H_H_EL"]:}
    H_P_CX = {idl_vars["H_P_CX"]:}
    H_P_EL = {idl_vars["H_P_EL"]:}
    Simple_CX = {idl_vars["Simple_CX"]:}

    ; now run KN1D
    kn1d,x,x_lim,xsep,gaugeH2,mu,Ti,Te,dens,vx,lc,dPipe, xh2,nh2,gammaxh2,th2,qxh2_total,nhp,thp,sh,sp, xh,nh,gammaxh,th,qxh_total,nethsource,sion,qh_total,sidewallh,lyman,balmer,gammahlim, debrief=1, truncate={idl_vars["truncate"]}, KN1D_dir='{idl_vars["KN1D_path"]}', Outdir='{idl_vars["Outdir"]:}/KN1D/'

    ; save result to an IDL .sav file
    save,xh2,nh2,gammaxh2,th2,qxh2_total,nhp,thp,sh,sp, xh,nh,gammaxh,th,qxh_total,nethsource,sion,qh_total,sidewallh,lyman,balmer,gammahlim,filename = "kn1d_out.sav"

    exit

    """

    # write IDL file
    final_directory = os.path.join(cwd, r'KN1D')
    if not os.path.exists(final_directory):
        os.makedirs(final_directory)
    with open(f"{cwd}/KN1D/kn1d_run_script.pro", "w") as f:
        f.write(idl_cmd)

    # Run the script
    os.system(f"cd {cwd}/KN1D; idl kn1d_run_script.pro")
    '''result = subprocess.run(
        f"cd {thisdir}/KN1D && idl kn1d_run_script.pro",
        shell=True,  # Use shell=True to allow shell commands like `cd`
        check=True,  # Raise an exception if the command fails
        text=True,   # Ensure output is treated as text
        capture_output=True  # Capture stdout and stderr for debugging
    )'''

    #### store all KN1D data for postprocessing  #####
    res = {}
    # res['ins'] = kn1d  # store inputs for plotting
    out = res["out"] = scipy.io.readsav(f"{cwd}/KN1D/kn1d_out.sav")
    res["kn1d_input"] = scipy.io.readsav(f"{cwd}/KN1D/.KN1D_input")
    res["kn1d_mesh"] = scipy.io.readsav(f"{cwd}/KN1D/.KN1D_mesh")
    res["kn1d_H2"] = scipy.io.readsav(f"{cwd}/KN1D/.KN1D_H2")
    res["kn1d_H"] = scipy.io.readsav(f"{cwd}/KN1D/.KN1D_H")

    os.chdir(cwd)

    # ---------------------------
    # Additional processed outputs
    # Compute ion flux by integrating over atomic ionization rate
    Sion = out["sion"]
    Sion_interp = interp1d(out["xh"], Sion, bounds_error=False, fill_value=0.0)(
        kn1d["x"]
    )
    out["Gamma_i"] = cumtrapz(Sion_interp, kn1d["x"], initial=0.0)

    # Effective diffusivity
    gradient_ne = np.gradient(np.log(kn1d["ne_m3"]), kn1d["x"])
    gradient_ne_safe = np.where(gradient_ne == 0, np.inf, gradient_ne)
    out["D_eff"] = np.abs(out["Gamma_i"] / gradient_ne_safe)


    # ensure that x bases are all to the same accuracy to avoid issues in interpolation
    out["xh"] = round_arr(out["xh"], num, dtype=float)
    out["xh2"] = round_arr(out["xh2"], num, dtype=float)

    # gradient length scales
    #out["L_ne"] = np.abs(1.0 / np.gradient(np.log(kn1d["ne_m3"]), kn1d["x"]))
    #out["L_Te"] = np.abs(1.0 / np.gradient(np.log(kn1d["Te_eV"]), kn1d["x"]))
    #out["L_Ti"] = np.abs(1.0 / np.gradient(np.log(kn1d["Ti_eV"]), kn1d["x"]))
    gradient_log_ne = np.gradient(np.log(kn1d["ne_m3"]), kn1d["x"])
    gradient_log_ne_safe = np.where(gradient_log_ne == 0, np.NaN, gradient_log_ne)  # Replace zeros with infinity
    out["L_ne"] = np.abs(1.0 / gradient_log_ne_safe)

    gradient_Te = np.gradient(np.log(kn1d["Te_eV"]), kn1d["x"])
    gradient_Te_safe = np.where(gradient_Te == 0, np.NaN, gradient_Te)  # Replace zeros with infinity
    out["L_Te"] = np.abs(1.0 / gradient_Te_safe)

    gradient_Ti = np.gradient(np.log(kn1d["Ti_eV"]), kn1d["x"])
    gradient_Ti_safe = np.where(gradient_Ti == 0, np.NaN, gradient_Ti)  # Replace zeros with infinity
    out["L_Ti"] = np.abs(1.0 / gradient_Ti_safe)



    #### Calculate radial profiles of neutral excited states   ####

    # take KN1D neutral density profile to be the ground state (excited states are a small correction wrt this)
    N1 = interp1d(out["xh"], out["nh"], kind="linear", fill_value=0.0, bounds_error=False)(kn1d["x"])
    M1 = interp1d(out["xh2"], out["nh2"], kind="linear", fill_value=0.0, bounds_error=False)(kn1d["x"])

    # assume pure plasma and quasi-neutrality
    nhp_interp = interp1d(out["xh2"], out["nhp"], bounds_error=False, fill_value=0.0)(
        kn1d["x"]
    )  # nH2+
    out["ni"] = kn1d["ne_m3"] - nhp_interp

    # get profiles of excited states' density (only n=2 and n=3) ---- densities in cm^-3, Te in eV
    N2, N2_ground, N2_cont = neutrals.get_exc_state_ratio(
        m=2,
        N1=N1 / 1e6,
        ni=out["ni"] / 1e6,
        ne=kn1d["ne_m3"] / 1e6,
        Te=kn1d["Te_eV"],
        plot=False,
        rad_prof=kn1d["x"],
    )
    N3, N3_ground, N3_cont = neutrals.get_exc_state_ratio(
        m=3,
        N1=N1 / 1e6,
        ni=out["ni"] / 1e6,
        ne=kn1d["ne_m3"] / 1e6,
        Te=kn1d["Te_eV"],
        plot=False,
        rad_prof=kn1d["x"],
    )

    out["N2"] = N2 * 1e6  # transform back to m^-3 (all KN1D units are in SI units)
    out["N2_ground"] = N2_ground * 1e6
    out["N2_cont"] = N2_cont * 1e6
    out["N3"] = N3 * 1e6
    out["N3_ground"] = N3_ground * 1e6
    out["N3_cont"] = N3_cont * 1e6

    # store rwall_m to allow external coordinate conversions
    out["rwall_m"] = wall_m

    #################################
    # Store neutral density profiles in a format that can be used for integrated modeling
    wrapper_input = res["wrapper_input"] = {}

    wrapper_input['x'] = x_to_wall_m
    wrapper_input['psi_n'] = psi_n

    wrapper_input['ne_m3'] = _ne_m3
    wrapper_input['Te_eV'] = _Te_eV
    wrapper_input['Ti_eV'] = _Ti_eV

    wrapper_input['p_H2_mTorr'] = p_H2_mTorr
    wrapper_input['p_H2_Pa'] = p_H2_Pa

    wrapper_input['clen_divertor_m'] = clen_divertor_m
    wrapper_input['clen_limiter_m'] = clen_limiter_m

    wrapper_input['sep_m'] = sep_m
    wrapper_input['lim_m'] = lim_m
    wrapper_input['wall_m'] = wall_m
    wrapper_input['innermost_x_m'] = innermost_x_m

    wrapper_input['mu'] = mu
    wrapper_input['pipe_diag_m'] = pipe_diag_m
    wrapper_input['collisions'] = collisions
    wrapper_input['near_far_SOL_boundary_m'] = near_far_SOL_boundary_m
    wrapper_input['ne_min_m3'] = ne_min_m3
    wrapper_input['Te_min_eV'] = Te_min_eV
    wrapper_input['Ti_min_eV'] = Ti_min_eV

    wrapper_output = res["wrapper_output"] = {}

    wrapper_output['x'] = x_to_wall_m
    wrapper_output['psi_n'] = psi_n
    
    wrapper_output['T_H'] = interp1d(out['rwall_m']-out['xh'], out['th'], kind="linear", fill_value=np.NaN, bounds_error=False)(x_to_wall_m)
    wrapper_output['T_H2'] = interp1d(out['rwall_m']-out['xh2'], out['th2'], kind="linear", fill_value=np.NaN, bounds_error=False)(x_to_wall_m)

    wrapper_output['n_H'] = interp1d(out['rwall_m']-out['xh'], out['nh'], kind="linear", fill_value=np.NaN, bounds_error=False)(x_to_wall_m)
    wrapper_output['n_H2'] = interp1d(out['rwall_m']-out['xh2'], out['nh2'], kind="linear", fill_value=np.NaN, bounds_error=False)(x_to_wall_m)

    wrapper_output['sh'] = interp1d(out['rwall_m']-out['xh2'], out['sh'], kind="linear", fill_value=np.NaN, bounds_error=False)(x_to_wall_m)
    wrapper_output['sp'] = interp1d(out['rwall_m']-out['xh2'], out['sp'], kind="linear", fill_value=np.NaN, bounds_error=False)(x_to_wall_m)
    wrapper_output['Sion'] = interp1d(out['rwall_m']-out['xh'], out['Sion'], kind="linear", fill_value=np.NaN, bounds_error=False)(x_to_wall_m)

    wrapper_output['balmer'] = interp1d(out['rwall_m']-out['xh'], out['balmer'], kind="linear", fill_value=np.NaN, bounds_error=False)(x_to_wall_m)
    wrapper_output['lyman'] = interp1d(out['rwall_m']-out['xh'], out['lyman'], kind="linear", fill_value=np.NaN, bounds_error=False)(x_to_wall_m)

    wrapper_output['gammaxh'] = interp1d(out['rwall_m']-out['xh'], out['gammaxh'], kind="linear", fill_value=np.NaN, bounds_error=False)(x_to_wall_m)
    wrapper_output['gammaxh2'] = interp1d(out['rwall_m']-out['xh2'], out['gammaxh2'], kind="linear", fill_value=np.NaN, bounds_error=False)(x_to_wall_m)

    wrapper_output['QH_total'] = interp1d(out['rwall_m']-out['xh'], out['QH_total'], kind="linear", fill_value=np.NaN, bounds_error=False)(x_to_wall_m)
    sep_i = np.searchsorted(wrapper_output['x'], wrapper_input['sep_m'])
    rate_sion = cumtrapz(wrapper_output['Sion'], wrapper_output['x'], initial=0.0)
    wrapper_output['Sion_core'] = rate_sion[sep_i]
    wrapper_output['Sion_sol'] = rate_sion[-1]-rate_sion[sep_i]

    

    '''# save profiles on (inverted) grid extending all the way to the axis (extrapolating)
    #_rhop = coords.rad_coord_transform(
    #    wall_m - kn1d["x"][::-1], "Rmid", "rhop", geqdsk
    #)
    out_profs["rhop"] = np.linspace(np.min(_rhop), np.max(_rhop), 200)
    out_profs["x"] = coords.rad_coord_transform(out_profs["rhop"], "rhop", "Rmid", geqdsk)

    M1_safe = np.where(M1 > 0, M1, np.nan)
    out_profs["m0"] = np.exp(
        interp1d(_rhop, np.log(M1_safe[::-1]), bounds_error=False, fill_value="extrapolate")(
            out_profs["rhop"]
        )
    )
    N1_safe = np.where(N1 > 0, N1, np.nan)
    out_profs["n0"] = np.exp(
        interp1d(_rhop, np.log(N1_safe[::-1]), bounds_error=False, fill_value="extrapolate")(
            out_profs["rhop"]
        )
    )
    N2_safe = np.where(out["N2"] > 0, out["N2"], np.nan)
    out_profs["n0_n2"] = np.exp(
        interp1d(
            _rhop, np.log(N2_safe[::-1]), bounds_error=False, fill_value="extrapolate"
        )(out_profs["rhop"])
    )
    N3_safe = np.where(out["N3"] > 0, out["N3"], np.nan)
    out_profs["n0_n3"] = np.exp(
        interp1d(
            _rhop, np.log(N3_safe[::-1]), bounds_error=False, fill_value="extrapolate"
        )(out_profs["rhop"])
    )

    # also save profiles of Ly- and H/D-alpha
    _rhop_emiss = coords.rad_coord_transform(
        wall_m - out["xh"][::-1], "rmid", "rhop", geqdsk
    )
    lyman_safe = np.where((out["lyman"] > 0) & (out["lyman"] < 1e31), out["lyman"], np.nan)
    out['lyman'] = lyman_safe
    out_profs["lyman"] = np.exp(
        interp1d(
            _rhop_emiss,
            np.log(lyman_safe[::-1]),
            bounds_error=False,
            fill_value="extrapolate",
        )(out_profs["rhop"])
    )
    balmer_safe = np.where((out["balmer"] > 0) & (out['balmer']<1e31), out["balmer"], np.nan)
    out['balmer'] = balmer_safe
    out_profs["balmer"] = np.exp(
        interp1d(
            _rhop_emiss,
            np.log(balmer_safe[::-1]),
            bounds_error=False,
            fill_value="extrapolate",
        )(out_profs["rhop"])
    )'''
    print("Finished in: ", time.time()-t)
    return res

def plot_input_kin_prof_x(
    x_to_wall_m, ne_m3, Te_eV, Ti_eV, innermost_x_m, sep_m, lim_m
):
    """Plot extent of kinetic profiles entering KN1D calculation"""

    indin = np.argmin(
        np.abs(
            x_to_wall_m - (np.max(x_to_wall_m) - innermost_x_m)
        )
    )

    fig, axs = plt.subplots(3, 1, figsize=(8, 8), sharex=True)
    axs[0].plot(x_to_wall_m, ne_m3)
    axs[1].plot(x_to_wall_m, Te_eV)
    axs[2].plot(x_to_wall_m, Ti_eV)
    axs[-1].set_xlabel(r"$r_{mid}$ [m]")
    axs[0].set_ylabel(r"$n_e$ [$m^{-3}$]")
    axs[1].set_ylabel(r"$T_e$ [$eV$]")
    axs[2].set_ylabel(r"$T_i$ [$eV$]")

    axs[0].set_ylim([0, np.max(ne_m3[indin:])])
    #axs[0].set_ylim([0, np.max(ne_m3)*1.05])
    axs[0].grid(True)
    axs[1].set_ylim([0, np.max(Te_eV[indin:])])
    #axs[1].set_ylim([0, np.max(Te_eV)*1.05])
    axs[1].grid(True)
    axs[2].set_ylim([0, np.max(Ti_eV[indin:])])
    #axs[2].set_ylim([0, np.max(Ti_eV)*1.05])
    axs[2].grid(True)
    axs[0].set_xlim(
        [
            innermost_x_m,
            np.max(x_to_wall_m),
        ]
    )
    plt.subplots_adjust(hspace=0)

    # also show location of limiter and LCFS:
    axs[0].axvline(sep_m, c="gray", linestyle="--")
    axs[1].axvline(sep_m, c="gray", linestyle="--")
    axs[2].axvline(sep_m, c="gray", linestyle="--")

    axs[0].axvline(lim_m, c="gray", linestyle=":")
    axs[1].axvline(lim_m, c="gray", linestyle=":")
    axs[2].axvline(lim_m, c="gray", linestyle=":")

def plot_overview_x(res):
    #ins = res["kn1d_input"]
    #outs = res["out"]

    mu = int(res["kn1d_input"]["mu"])
    species = "H" if mu == 1 else "D"

    fig, ax = plt.subplots(2, 2, figsize=(10, 10))
    ax[0,0].set_title("Temperature")
    ax[0,0].plot(res["wrapper_input"]["x"], res["wrapper_input"]['Te_eV'], color="gray", linestyle="--", label="$T_e$")
    ax[0,0].plot(res["wrapper_input"]["x"], res["wrapper_input"]['Ti_eV'], color="gray", linestyle="-.", label="$T_i$")
    ax[0,0].plot(res["wrapper_output"]["x"], res["wrapper_output"]['T_H'], color="red", linestyle="-", label="$T_D$")
    ax[0,0].plot(res["wrapper_output"]["x"], res["wrapper_output"]['T_H2'], color="blue", linestyle="-", label="$T_{D2}$")
    ax[0,0].set_ylabel("T [eV]")

    ax[0,1].set_title("Radiation")
    #ax[0,1].plot(res["out"]["rwall_m"]-res["kn1d_input"]["x"], res["kn1d_input"]['n'], color="gray", linestyle="--", label="$n_e$")
    #ax[0,1].plot(res["out"]["rwall_m"]-res["out"]["xh"], res["out"]['nh']*1e6, color="red", linestyle="-", label="$n_D$")
    #ax[0,1].plot(res["out"]["rwall_m"]-res["out"]["xh2"], res["out"]['nh2']*1e6, color="blue", linestyle="-", label="$n_{D2}$")
    mask = res["wrapper_output"]["balmer"]<1e7
    x = res["wrapper_output"]["x"][mask]
    balmer = res["wrapper_output"]["balmer"][mask]

    ax[0,1].plot(x, balmer, "-", color="magenta", label="balmer (JH)")

    mask = res["wrapper_output"]["lyman"]<1e7
    x = res["wrapper_output"]["x"][mask]
    lyman = res["wrapper_output"]["lyman"][mask]

    ax[0,1].plot(x, lyman, "-", color="purple", label="lyman (JH)")
    ax[0,1].set_ylabel("Rad [W m$^{-3}$]")
    ax[0,1].set_yscale('log')
    #ax[0,1].plot(res["out"]["rwall_m"]-res["out"]["xh"], res["out"]["lyman"], "-", color="purple", label="lyman (JH)")
    #ax[1,0].set_xlim(res["out"]["rwall_m"]-np.max(res["out"]["xh"]),res["out"]["rwall_m"]-np.min(res["out"]["xh"]))

    ax[1,0].set_title("Source")
    #ax[1,0].plot(res["out"]["rwall_m"]-res["out"]["xh"], res["out"]['sion'], color="red", linestyle="-", label="Atomic ionization rate")
    ax[1,0].plot(res["wrapper_output"]["x"], res["wrapper_output"]["sh"], color="red", label="Atomic source (sh)")
    ax[1,0].plot(res["wrapper_output"]["x"], res["wrapper_output"]["sp"], color="blue", label="Ion source (sp)")
    ax[1,0].plot(res["wrapper_output"]["x"], res["wrapper_output"]["Sion"], color="green", label="Ion source (Sion)")
    ax[1,0].plot(res["wrapper_input"]["wall_m"]-res["kn1d_H2"]["xh2"], res["kn1d_H2"]["sph2"], color="orange", label="Molecular source (sph2)")
    #ax[1,0].set_xlim(res["out"]["rwall_m"]-np.max(res["out"]["xh"]),res["out"]["rwall_m"]-np.min(res["out"]["xh"]))
    ax[1,0].set_ylabel("Source [$m^{-3} s^{-1}$]")

    ax[1,1].set_title("log(Density)")
    ax[1,1].plot(res["wrapper_input"]["x"], res["wrapper_input"]['ne_m3'], color="gray", linestyle="--", label="$n_e$")
    ax[1,1].plot(res["wrapper_output"]["x"], res["wrapper_output"]['n_H'], color="red", linestyle="-", label="$n_D$")
    ax[1,1].plot(res["wrapper_output"]["x"], res["wrapper_output"]['n_H2'], color="blue", linestyle="-", label="$n_{D2}$")
    ax[1,1].set_yscale('log')
    ax[1,1].set_ylabel("Density [$m^{-3}$]")
    '''ax[1,1].set_ylim(max(1e10,min(np.min(res["wrapper_input"]['ne_m3']),
                        np.min(res["wrapper_output"]['n_H']),
                        np.min(res["wrapper_output"]['n_H2']))/2)
                    , max(np.max(res["wrapper_input"]['ne_m3']),
                        np.max(res["wrapper_output"]['n_H']),
                        np.max(res["wrapper_output"]['n_H2']))*2)'''


    for a in ax.flatten():
        a.grid()
        a.legend()
        a.set_xlim(res['wrapper_input']['innermost_x_m'], res['wrapper_input']['wall_m'])
        a.axvline(res['wrapper_input']['sep_m'], linestyle="--", color="gray")
        a.axvline(res['wrapper_input']['lim_m'], linestyle="--", color="gray")
        a.set_xlabel("R [m]")

'''
END OF CUSTOM FUNCTIONS
'''

def _setup_kin_profs(
    rhop,
    ne_cm3_in,
    Te_eV_in,
    Ti_eV_in,
    geqdsk,
    bound_sep_cm,
    lim_sep_cm,
    kin_prof_exp_decay_SOL=False,
    kin_prof_exp_decay_LS=False,
    ne_decay_len_cm=1.0,
    Te_decay_len_cm=1.0,
    Ti_decay_len_cm=1.0,
    ne_min_cm3=1e12,
    Te_min_eV=1.0,
    Ti_min_eV=1.0,
):
    """Private method to set up kinetic profiles to the format required by
    :py:func:`~aurora.kn1d.run_kn1d`. Refer to this function for descriptions of inputs.

    This function returns ne, Te and Ti profiles on the rmid_to_wall_cm radial grid,
    from the core to the wall.

    Parameters
    ----------
    rhop : 1D array
        Sqrt of poloidal flux grid on which ne_cm3, Te_eV and Ti_eV are given.
    ne_cm3_in : 1D array
        Electron density on rhop grid [:math:`cm^{-3}`].
    Te_eV_in : 1D array
        Electron temperature on rhop grid [:math:`eV`].
    Ti_eV_in : 1D array
        Main ion temperature on rhop grid [:math:`eV`].
    geqdsk : `omfit_classes.omfit_eqdsk.OMFITgeqdsk` class instance
        gEQDSK file as processed by the `omfit_classes.omfit_eqdsk.OMFITgeqdsk` class.
    bound_sep_cm : float
        Distance between the wall/boundary and the separatrix [:math:`cm`].
    lim_sep_cm : float
        Distance between the limiter and the separatrix [:math:`cm`].
    kin_prof_exp_decay_SOL : bool
        If True, kinetic profiles are set to exponentially decay over the SOL region.
    kin_prof_exp_decay_LS : bool
        If True, kinetic profiles are set to exponentially decay over the LS region.
    ne_decay_len_cm : list of 2 float
        Exponential decay lengths of electron density in the SOL and LS regions.
        Default is [1,1] :math:`cm`.
    Te_decay_len_cm : float
        Exponential decay lengths of electron temperature in the SOL and LS regions.
        Default is [1,1] :math:`cm`.
    Ti_decay_len_cm : float
        Exponential decay lengths of main ion temperature in the SOL and LS regions.
        Default is [1,1] :math:`cm`.
    ne_min_cm3 : float
        Minimum electron density across profile. Default is :math:`10^{12} cm^{-3}`.
    Te_min_eV : float
        Minimum electron temperaure across profile. Default is :math:`eV`.
    Ti_min_eV : float
        Minimum main ion temperaure across profile. Default is :math:`eV`.

    Returns
    -------
    rmid_to_wall_cm : 1D array
        Midradius coordinate from the magnetic axis to the wall. Units of [:math:`cm`].
    ne_cm3 : 1D array
        Electron density [:math:`cm^{-3}`] on the rmid_to_wall_cm grid.
    Te_eV : 1D array
        Electron temperature [:math:`eV`] on the rmid_to_wall_cm grid.
    Ti_eV : 1D array
        Main ion temperature [:math:`eV`] on the rmid_to_wall_cm grid.
    """

    # convert radial coordinate to rmid
    rmid = coords.rad_coord_transform(rhop, "rhop", "rmid", geqdsk)  # m

    # define radial regions in the SOL in coordinates centered on the mag axis
    rsep = coords.rad_coord_transform(1.0, "rhop", "rmid", geqdsk)
    rwall = rsep + bound_sep_cm * 1e-2  # cm-->m
    rlim = rsep + lim_sep_cm * 1e-2  # cm-->m

    # interpolate profiles on grid extending to wall
    rmid_to_wall_m = np.linspace(np.min(rmid), rwall, 1001)  # 101) #201)
    _ne_cm3 = interp1d(rmid, ne_cm3_in, bounds_error=False)(
        rmid_to_wall_m
    )  # extrapolates to nan
    _Te_eV = interp1d(rmid, Te_eV_in, bounds_error=False)(
        rmid_to_wall_m
    )  # extrapolates to nan
    _Ti_eV = interp1d(rmid, Ti_eV_in, bounds_error=False)(
        rmid_to_wall_m
    )  # extrapolates to nan

    indLCFS = np.searchsorted(rmid_to_wall_m, rsep)
    indLS = np.searchsorted(rmid_to_wall_m, rlim)
    ind_end = np.searchsorted(rmid_to_wall_m, rmid[-1])

    # if kinetic profiles don't extend far enough in radius, we must set an exp decay depending on the radial region
    if ind_end < indLS:
        # decays in SOL (all the way to the wall)
        ne_cm3_sol = _ne_cm3[ind_end - 1] * np.exp(
            -(rmid_to_wall_m[ind_end:] - rmid_to_wall_m[ind_end - 1])
            / (ne_decay_len_cm[0] / 100.0)
        )
        ne_cm3_ = np.concatenate((_ne_cm3[:ind_end], ne_cm3_sol))
        Te_eV_sol = _Te_eV[ind_end - 1] * np.exp(
            -(rmid_to_wall_m[ind_end:] - rmid_to_wall_m[ind_end - 1])
            / (Te_decay_len_cm[0] / 100.0)
        )
        Te_eV_ = np.concatenate((_Te_eV[:ind_end], Te_eV_sol))
        Ti_eV_sol = _Ti_eV[ind_end - 1] * np.exp(
            -(rmid_to_wall_m[ind_end:] - rmid_to_wall_m[ind_end - 1])
            / (Ti_decay_len_cm[0] / 100.0)
        )
        Ti_eV_ = np.concatenate((_Ti_eV[:ind_end], Ti_eV_sol))
    else:
        ne_cm3_ = copy.deepcopy(_ne_cm3)
        Te_eV_ = copy.deepcopy(_Te_eV)
        Ti_eV_ = copy.deepcopy(_Ti_eV)

    if ind_end < len(rmid_to_wall_m):
        # decays in the LS
        ne_cm3_ls = ne_cm3_[ind_end - 1] * np.exp(
            -(rmid_to_wall_m[ind_end:] - rmid_to_wall_m[ind_end - 1])
            / (ne_decay_len_cm[1] / 100.0)
        )
        ne_cm3 = np.concatenate((ne_cm3_[:ind_end], ne_cm3_ls))
        Te_eV_ls = Te_eV_[ind_end - 1] * np.exp(
            -(rmid_to_wall_m[ind_end:] - rmid_to_wall_m[ind_end - 1])
            / (Te_decay_len_cm[1] / 100.0)
        )
        Te_eV = np.concatenate((Te_eV_[:ind_end], Te_eV_ls))
        Ti_eV_ls = Ti_eV_[ind_end - 1] * np.exp(
            -(rmid_to_wall_m[ind_end:] - rmid_to_wall_m[ind_end - 1])
            / (Ti_decay_len_cm[1] / 100.0)
        )
        Ti_eV = np.concatenate((Ti_eV_[:ind_end], Ti_eV_ls))
    else:
        ne_cm3 = copy.deepcopy(ne_cm3_)
        Te_eV = copy.deepcopy(Te_eV_)
        Ti_eV = copy.deepcopy(Ti_eV_)

    # User may want to set exp decay in SOL or LS in place of dubious experimental data
    if kin_prof_exp_decay_SOL:
        # decays in the SOL
        ne_cm3[indLCFS:indLS] = ne_cm3[indLCFS - 1] * np.exp(
            -(rmid_to_wall_m[indLCFS:indLS] - rmid_to_wall_m[indLCFS - 1])
            / (ne_decay_len_cm[0] / 100.0)
        )
        Te_eV[indLCFS:indLS] = Te_eV[indLCFS - 1] * np.exp(
            -(rmid_to_wall_m[indLCFS:indLS] - rmid_to_wall_m[indLCFS - 1])
            / (Te_decay_len_cm[0] / 100.0)
        )
        Ti_eV[indLCFS:indLS] = Ti_eV[indLCFS - 1] * np.exp(
            -(rmid_to_wall_m[indLCFS:indLS] - rmid_to_wall_m[indLCFS - 1])
            / (Ti_decay_len_cm[0] / 100.0)
        )

    if kin_prof_exp_decay_LS:
        # decays in the LS
        ne_cm3[indLS:] = ne_cm3[indLS - 1] * np.exp(
            -(rmid_to_wall_m[indLS:] - rmid_to_wall_m[indLS - 1])
            / (ne_decay_len_cm[1] / 100.0)
        )
        Te_eV[indLS:] = Te_eV[indLS - 1] * np.exp(
            -(rmid_to_wall_m[indLS:] - rmid_to_wall_m[indLS - 1])
            / (Te_decay_len_cm[1] / 100.0)
        )
        Ti_eV[indLS:] = Ti_eV[indLS - 1] * np.exp(
            -(rmid_to_wall_m[indLS:] - rmid_to_wall_m[indLS - 1])
            / (Ti_decay_len_cm[1] / 100.0)
        )

    # set minima across radial profiles
    ne_cm3[ne_cm3 < ne_min_cm3] = ne_min_cm3
    Te_eV[Te_eV < Te_min_eV] = Te_min_eV
    Ti_eV[Ti_eV < Ti_min_eV] = Ti_min_eV

    return rmid_to_wall_m, ne_cm3, Te_eV, Ti_eV


def run_kn1d(
    rhop,
    ne_cm3,
    Te_eV,
    Ti_eV,
    geqdsk,
    p_H2_mTorr,
    clen_divertor_cm,
    clen_limiter_cm,
    bound_sep_cm,
    lim_sep_cm,
    innermost_rmid_cm=5.0,
    mu=2.0,
    pipe_diag_cm=0.0,
    vx=0.0,
    collisions={},
    kin_prof_exp_decay_SOL=False,
    kin_prof_exp_decay_LS=False,
    ne_decay_len_cm=[1.0, 1.0],
    Te_decay_len_cm=[1.0, 1.0],
    Ti_decay_len_cm=[1.0, 1.0],
    ne_min_cm3=1e12,
    Te_min_eV=1.0,
    Ti_min_eV=1.0,
    plot_kin_profs=False,
):
    """Run KN1D for the given parameters. Refer to the KN1D manual for details.

    Depending on the provided options, kinetic profiles are extended beyond the Last Closed
    Flux Surface (LCxFS) and the Limiter Shadow (LS) via exponential decays with specified
    decay lengths. It is assumed that the given kinetic profiles extend from the core until
    at least the LCFS. All inputs are taken to be time-independent.

    This function automatically checks if a KN1D repository is available; if it is not,
    it obtains it from the web and compiles the necessary code.

    Note that an IDL license must be available. Aurora does not currently include a Python
    translation of KN1D -- it only acts as a wrapper.

    Parameters
    ----------
    rhop : 1D array
        Sqrt of poloidal flux grid on which ne_cm3, Te_eV and Ti_eV are given.
    ne_cm3 : 1D array
        Electron density on rhop grid [:math:`cm^{-3}`].
    Te_eV : 1D array
        Electron temperature on rhop grid [:math:`eV`].
    Ti_eV : 1D array
        Main ion temperature on rhop grid [:math:`eV`].
    geqdsk : `omfit_classes.omfit_eqdsk.OMFITgeqdsk` class instance
        gEQDSK file as processed by the `omfit_classes.omfit_eqdsk.OMFITgeqdsk` class.
    p_H2_mTorr : float
        Pressure of molecular hydrogen-isotopes measured at the wall. This may be estimated
        from experimental pressure gauges. This variable effectively sets the amplitude of the
        neutral source at the edge. Units of :math:`mTorr`.
    clen_divertor_cm : float
        Connection length from the midplane to the divertor [:math:`cm`].
    clen_limiter_cm : float
        Connection length from the midplane to the limiter [:math:`cm`].
    bound_sep_cm : float
        Distance between the wall/boundary and the separatrix [:math:`cm`].
    lim_sep_cm : float
        Distance between the limiter and the separatrix [:math:`cm`].
    innermost_rmid_cm : float
        Distance from the wall to solve for. Default is 5 cm.
    mu : float
        Atomic mass number of simulated species. Default is 2.0 (D).
    pipe_diag_cm : float
        Diameter of the pipe through which H2 pressure is measured (see `p_H2_mTorr` variable).
        If left to 0, this diameter is effectively set to infinity. Default is 0.
    vx : float
        Radial velocity imposed on neutrals. This only has a weak effect usually.
        Default is 0 [:math:`cm/s`].
    collisions : dict
        Collision terms flags. Set each to True or False. If any of the flags are not given,
        all collision terms are internally set to be active. Possible flags are
        'H2_H2_EL','H2_P_EL','H2_H_EL','H2_HP_CX','H_H_EL','H_P_CX','H_P_EL','Simple_CX'
    kin_prof_exp_decay_SOL : bool
        If True, kinetic profiles are set to exponentially decay over the SOL region.
    kin_prof_exp_decay_LS : bool
        If True, kinetic profiles are set to exponentially decay over the LS region.
    ne_decay_len_cm : list of 2 float
        Exponential decay lengths of electron density in the SOL and LS regions.
        Default is [1,1] :math:`cm`.
    Te_decay_len_cm : float
        Exponential decay lengths of electron temperature in the SOL and LS regions.
        Default is [1,1] :math:`cm`.
    Ti_decay_len_cm : float
        Exponential decay lengths of main ion temperature in the SOL and LS regions.
        Default is [1,1] :math:`cm`.
    ne_min_cm3 : float
        Minimum electron density across profile. Default is :math:`10^{12} cm^{-3}`.
    Te_min_eV : float
        Minimum electron temperaure across profile. Default is :math:`eV`.
    Ti_min_eV : float
        Minimum main ion temperaure across profile. Default is :math:`eV`.
    plot_kin_profs : bool
        If True, kinetic profiles input to KN1D are plotted.

    Returns
    -------
    dict
        KN1D results and inputs, all collected into a dictionary. See example script for
        an illustration of using this.

    Notes
    -----
    For an example application, see the examples/aurora_kn1d.py script.
    """

    if "IDL_STARTUP" not in os.environ and "IDL_HOME" not in os.environ:
        raise ValueError(
            "An IDL installation does not seem to be available! KN1D cannot be run."
        )

    cwd = os.getcwd()

    # make sure that the KN1D source code is accessible.
    if "KN1D" not in os.listdir(thisdir):
        # if 'KN1D_DIR' not in os.environ:
        # git clone the KN1D repository
        #os.system(f"git clone https://repo.tok.ipp.cas.cz/svorc/kn1d-custom {thisdir}/KN1D")

        #os.chdir(f"{thisdir}/KN1D")
        # compile fortran libraries
        #print(f"export KN1D_DIR={thisdir}/KN1D; make clean; make")
        #os.system(f"export KN1D_DIR={thisdir}/KN1D; make clean; make")
        #os.chdir(cwd)
        pass
        # else:
        # copy KN1D directory locally
        # shutil.copytree(os.environ['KN1D_DIR'],thisdir+'/KN1D')
    else:
        # KN1D directory already available, assumed to be already built
        # NB: users need to have write-access to this directory!
        pass

    kn1d = {}
    kn1d.update(collisions)
    if "H2_H2_EL" not in kn1d:
        kn1d["H2_H2_EL"] = True
    if "H2_P_EL" not in kn1d:
        kn1d["H2_P_EL"] = True
    if "H2_H_EL" not in kn1d:
        kn1d["H2_H_EL"] = True
    if "H2_HP_CX" not in kn1d:
        kn1d["H2_HP_CX"] = True
    if "H_H_EL" not in kn1d:
        kn1d["H_H_EL"] = True
    if "H_P_CX" not in kn1d:
        kn1d["H_P_CX"] = True
    if "H_P_EL" not in kn1d:
        kn1d["H_P_EL"] = True
    if "Simple_CX" not in kn1d:
        kn1d["Simple_CX"] = False

    # get kinetic profiles on rmid_to_wall_m grid, applying exponential decays in SOL as requested
    rmid_to_wall_m, _ne_cm3, _Te_eV, _Ti_eV = _setup_kin_profs(
        rhop,
        ne_cm3,
        Te_eV,
        Ti_eV,
        geqdsk,
        bound_sep_cm,
        lim_sep_cm,
        kin_prof_exp_decay_SOL,
        kin_prof_exp_decay_LS,
        ne_decay_len_cm,
        Te_decay_len_cm,
        Ti_decay_len_cm,
        ne_min_cm3,
        Te_min_eV,
        Ti_min_eV,
    )

    if plot_kin_profs:
        # show kinetic profiles going into KN1D modeling
        plot_input_kin_prof(
            rmid_to_wall_m,
            _ne_cm3,
            _Te_eV,
            _Ti_eV,
            innermost_rmid_cm,
            bound_sep_cm,
            lim_sep_cm,
        )

    rhop = coords.rad_coord_transform(rmid_to_wall_m, "rmid", "rhop", geqdsk)
    rwall_m = rmid_to_wall_m[-1]  # m
    rsep_m = coords.rad_coord_transform(1.0, "rhop", "rmid", geqdsk)  # m
    rlim_m = rsep_m + lim_sep_cm * 1e-2  # m

    # KN1D defines coordinates from the wall INWARD. Invert now:
    r_kn1d_m = np.abs(rmid_to_wall_m - rwall_m)[::-1]
    rlim_kn1d_m = np.abs(rlim_m - rwall_m)
    rsep_kn1d_m = np.abs(rsep_m - rwall_m)

    # diameter of pressure gauge pipe. Allows collisions with side-walls to be simulated
    dPipe = (
        pipe_diag_cm * np.ones(len(rmid_to_wall_m)) * 1e-2
    )  # m  -- zero values are treated as infinity

    # define the connection length vector
    lc = np.zeros(len(rmid_to_wall_m))
    lc[(rsep_m < rmid_to_wall_m) * (rmid_to_wall_m < rlim_m)] = (
        clen_divertor_cm * 1e-2
    )  # m
    lc[rmid_to_wall_m > rlim_m] = clen_limiter_cm * 1e-2  # m

    def idl_array(arr, num):
        # set arrays into string form for IDL format
        return "[" + ",".join([str(round(val, num)) for val in arr]) + "]"

    def round_arr(arr, num, dtype=str):
        # avoid issues with floating-point precision
        return np.array([round(val, num) for val in arr], dtype=dtype)

    # cut all radial profiles to given innermost location
    ind_in = np.searchsorted(
        r_kn1d_m, innermost_rmid_cm * 1e-2
    )  # r_kn1d_m is from wall inwards

    # Save arrays in final forms for KN1D
    num = 5  # digital point precision
    kn1d["x"] = round_arr(r_kn1d_m[:ind_in], num, float)
    ne_ = _ne_cm3[::-1] * 1e6
    kn1d["ne_m3"] = round_arr(ne_[:ind_in], num, float)  # m^-3
    Te_ = _Te_eV[::-1]
    kn1d["Te_eV"] = round_arr(Te_[:ind_in], num, float)  # eV
    Ti_ = _Ti_eV[::-1]
    kn1d["Ti_eV"] = round_arr(Ti_[:ind_in], num, float)  # eV
    dPipe_ = dPipe[::-1]
    kn1d["dPipe"] = round_arr(dPipe_[:ind_in], num, float)
    lc_ = lc[::-1]
    kn1d["lc"] = round_arr(lc_[:ind_in], num, float)
    kn1d["xlim"] = rlim_kn1d_m
    kn1d["xsep"] = rsep_kn1d_m
    kn1d["p_H2_mTorr"] = p_H2_mTorr  # mTorr
    kn1d["mu"] = mu

    # finally, set plasma radial velocity profile (negative is towards the wall [m s^-1])
    vx = np.ones_like(kn1d["x"]) * vx
    kn1d["vx"] = round_arr(vx, num, float)

    # Create IDL script to run KN1D
    idl_cmd = """
; Input data for KN1D run
x = {x:}
x_lim = {x_lim:.5f}
xsep = {xsep:.5f}
gaugeH2 = {gaugeH2:}
mu = {mu:}
Ti = {Ti:}
Te = {Te:}
dens = {ne:}
vx = {vx:}
lc = {lc:}
dPipe = {dPipe:}

; collisions options are set via a common block
common KN1D_collisions,H2_H2_EL,H2_P_EL,H2_H_EL,H2_HP_CX,H_H_EL,H_P_EL,H_P_CX,Simple_CX

H2_H2_EL= {H2_H2_EL:}
H2_P_EL = {H2_P_EL:}
H2_H_EL = {H2_H_EL:}
H_H_EL= {H_H_EL:}
H_P_CX = {H_P_CX:}
H_P_EL = {H_P_EL:}
Simple_CX = {Simple_CX:}

; now run KN1D
kn1d,x,x_lim,xsep,gaugeH2,mu,Ti,Te,dens,vx,lc,dPipe, xh2,nh2,gammaxh2,th2,qxh2_total,nhp,thp,sh,sp, xh,nh,gammaxh,th,qxh_total,nethsource,sion,qh_total,sidewallh,lyman,balmer,gammahlim

; save result to an IDL .sav file
save,xh2,nh2,gammaxh2,th2,qxh2_total,nhp,thp,sh,sp, xh,nh,gammaxh,th,qxh_total,nethsource,sion,qh_total,sidewallh,lyman,balmer,gammahlim,filename = "kn1d_out.sav"

exit

    """.format(
        x=idl_array(kn1d["x"], num),
        x_lim=kn1d["xlim"],
        xsep=kn1d["xsep"],
        gaugeH2=kn1d["p_H2_mTorr"],
        mu=kn1d["mu"],
        Ti=idl_array(kn1d["Ti_eV"], num),
        Te=idl_array(kn1d["Te_eV"], num),
        ne=idl_array(kn1d["ne_m3"], num),
        vx=idl_array(kn1d["vx"], num),
        lc=idl_array(kn1d["lc"], num),
        dPipe=idl_array(kn1d["dPipe"], num),
        H2_H2_EL=int(kn1d["H2_H2_EL"]),
        H2_P_EL=int(kn1d["H2_P_EL"]),
        H2_H_EL=int(kn1d["H2_H_EL"]),
        H_H_EL=int(kn1d["H_H_EL"]),
        H_P_CX=int(kn1d["H_P_CX"]),
        H_P_EL=int(kn1d["H_P_EL"]),
        Simple_CX=int(kn1d["Simple_CX"]),
    )

    # write IDL file
    with open(f"{thisdir}/KN1D/kn1d_run_script.pro", "w") as f:
        f.write(idl_cmd)

    # Run the script
    os.system(f"cd {thisdir}/KN1D; idl kn1d_run_script.pro")

    #### store all KN1D data for postprocessing  #####
    res = {}
    # res['ins'] = kn1d  # store inputs for plotting
    out = res["out"] = scipy.io.readsav(f"{thisdir}/KN1D/kn1d_out.sav")
    res["kn1d_input"] = scipy.io.readsav(f"{thisdir}/KN1D/.KN1D_input")
    res["kn1d_mesh"] = scipy.io.readsav(f"{thisdir}/KN1D/.KN1D_mesh")
    res["kn1d_H2"] = scipy.io.readsav(f"{thisdir}/KN1D/.KN1D_H2")
    res["kn1d_H"] = scipy.io.readsav(f"{thisdir}/KN1D/.KN1D_H")

    os.chdir(cwd)

    # ---------------------------
    # Additional processed outputs
    # Compute ion flux by integrating over atomic ionization rate
    Sion = out["sion"]
    Sion_interp = interp1d(out["xh"], Sion, bounds_error=False, fill_value=0.0)(
        kn1d["x"]
    )
    out["Gamma_i"] = cumtrapz(Sion_interp, kn1d["x"], initial=0.0)

    # Effective diffusivity
    out["D_eff"] = np.abs(
        out["Gamma_i"] / np.gradient(kn1d["ne_m3"], kn1d["x"])
    )  # check

    # ensure that x bases are all to the same accuracy to avoid issues in interpolation
    out["xh"] = round_arr(out["xh"], num, dtype=float)
    out["xh2"] = round_arr(out["xh2"], num, dtype=float)

    # gradient length scales
    #out["L_ne"] = np.abs(1.0 / np.gradient(np.log(kn1d["ne_m3"]), kn1d["x"]))
    #out["L_Te"] = np.abs(1.0 / np.gradient(np.log(kn1d["Te_eV"]), kn1d["x"]))
    #out["L_Ti"] = np.abs(1.0 / np.gradient(np.log(kn1d["Ti_eV"]), kn1d["x"]))

    gradient_ne = np.gradient(np.log(kn1d["ne_m3"]), kn1d["x"])
    gradient_ne_safe = np.where(gradient_ne == 0, np.inf, gradient_ne)  # Replace zeros with infinity
    out["L_ne"] = np.abs(1.0 / gradient_ne_safe)

    gradient_Te = np.gradient(np.log(kn1d["Te_eV"]), kn1d["x"])
    gradient_Te_safe = np.where(gradient_Te == 0, np.inf, gradient_Te)  # Replace zeros with infinity
    out["L_Te"] = np.abs(1.0 / gradient_Te_safe)

    gradient_Ti = np.gradient(np.log(kn1d["Ti_eV"]), kn1d["x"])
    gradient_Ti_safe = np.where(gradient_Ti == 0, np.inf, gradient_Ti)  # Replace zeros with infinity
    out["L_Ti"] = np.abs(1.0 / gradient_Ti_safe)

    #### Calculate radial profiles of neutral excited states   ####

    # take KN1D neutral density profile to be the ground state (excited states are a small correction wrt this)
    N1 = interp1d(out["xh"], out["nh"], kind="linear")(kn1d["x"])

    # assume pure plasma and quasi-neutrality
    nhp_interp = interp1d(out["xh2"], out["nhp"], bounds_error=False, fill_value=0.0)(
        kn1d["x"]
    )  # nH2+
    out["ni"] = kn1d["ne_m3"] - nhp_interp

    # get profiles of excited states' density (only n=2 and n=3) ---- densities in cm^-3, Te in eV
    N2, N2_ground, N2_cont = neutrals.get_exc_state_ratio(
        m=2,
        N1=N1 / 1e6,
        ni=out["ni"] / 1e6,
        ne=kn1d["ne_m3"] / 1e6,
        Te=kn1d["Te_eV"],
        plot=False,
        rad_prof=kn1d["x"],
    )
    N3, N3_ground, N3_cont = neutrals.get_exc_state_ratio(
        m=3,
        N1=N1 / 1e6,
        ni=out["ni"] / 1e6,
        ne=kn1d["ne_m3"] / 1e6,
        Te=kn1d["Te_eV"],
        plot=False,
        rad_prof=kn1d["x"],
    )

    out["N2"] = N2 * 1e6  # transform back to m^-3 (all KN1D units are in SI units)
    out["N2_ground"] = N2_ground * 1e6
    out["N2_cont"] = N2_cont * 1e6
    out["N3"] = N3 * 1e6
    out["N3_ground"] = N3_ground * 1e6
    out["N3_cont"] = N3_cont * 1e6

    # store rwall_m to allow external coordinate conversions
    out["rwall_m"] = rwall_m

    #################################
    # Store neutral density profiles in a format that can be used for integrated modeling
    out_profs = res["kn1d_profs"] = {}

    # save profiles on (inverted) grid extending all the way to the axis (extrapolating)
    _rhop = coords.rad_coord_transform(
        rwall_m - kn1d["x"][::-1], "rmid", "rhop", geqdsk
    )
    out_profs["rhop"] = np.linspace(0.0, 1.1, 200)

    out_profs["n0"] = np.exp(
        interp1d(_rhop, np.log(N1[::-1]), bounds_error=False, fill_value="extrapolate")(
            out_profs["rhop"]
        )
    )
    out_profs["n0_n2"] = np.exp(
        interp1d(
            _rhop, np.log(out["N2"][::-1]), bounds_error=False, fill_value="extrapolate"
        )(out_profs["rhop"])
    )
    out_profs["n0_n3"] = np.exp(
        interp1d(
            _rhop, np.log(out["N3"][::-1]), bounds_error=False, fill_value="extrapolate"
        )(out_profs["rhop"])
    )

    # also save profiles of Ly- and H/D-alpha
    _rhop_emiss = coords.rad_coord_transform(
        rwall_m - out["xh"][::-1], "rmid", "rhop", geqdsk
    )
    out_profs["lyman"] = np.exp(
        interp1d(
            _rhop_emiss,
            np.log(out["lyman"][::-1]),
            bounds_error=False,
            fill_value="extrapolate",
        )(out_profs["rhop"])
    )
    out_profs["balmer"] = np.exp(
        interp1d(
            _rhop_emiss,
            np.log(out["balmer"][::-1]),
            bounds_error=False,
            fill_value="extrapolate",
        )(out_profs["rhop"])
    )

    return res


def plot_input_kin_prof(
    rmid_to_wall_m, ne_cm3, Te_eV, Ti_eV, innermost_rmid_cm, bound_sep_cm, lim_sep_cm
):
    """Plot extent of kinetic profiles entering KN1D calculation"""
    fig, axs = plt.subplots(3, 1, figsize=(8, 8), sharex=True)
    axs[0].plot(rmid_to_wall_m * 1e2, ne_cm3)
    axs[1].plot(rmid_to_wall_m * 1e2, Te_eV)
    axs[2].plot(rmid_to_wall_m * 1e2, Ti_eV)
    axs[-1].set_xlabel(r"$r_{mid}$ [cm]")
    axs[0].set_ylabel(r"$n_e$ [$cm^{-3}$]")
    axs[1].set_ylabel(r"$T_e$ [$eV$]")
    axs[2].set_ylabel(r"$T_i$ [$eV$]")

    indin = np.argmin(
        np.abs(
            rmid_to_wall_m * 100 - (np.max(rmid_to_wall_m) * 100.0 - innermost_rmid_cm)
        )
    )

    axs[0].set_ylim([0, np.max(ne_cm3[indin:])])
    axs[0].grid(True)
    axs[1].set_ylim([0, np.max(Te_eV[indin:])])
    axs[1].grid(True)
    axs[2].set_ylim([0, np.max(Ti_eV[indin:])])
    axs[2].grid(True)
    axs[0].set_xlim(
        [
            np.max(rmid_to_wall_m) * 100.0 - innermost_rmid_cm,
            np.max(rmid_to_wall_m) * 100.0,
        ]
    )
    plt.subplots_adjust(hspace=0)

    # also show location of limiter and LCFS:
    axs[0].axvline(np.max(rmid_to_wall_m) * 1e2 - bound_sep_cm, c="m")
    axs[1].axvline(np.max(rmid_to_wall_m) * 1e2 - bound_sep_cm, c="m")
    axs[2].axvline(np.max(rmid_to_wall_m) * 1e2 - bound_sep_cm, c="m")

    axs[0].axvline(np.max(rmid_to_wall_m) * 1e2 - (bound_sep_cm - lim_sep_cm), c="m")
    axs[1].axvline(np.max(rmid_to_wall_m) * 1e2 - (bound_sep_cm - lim_sep_cm), c="m")
    axs[2].axvline(np.max(rmid_to_wall_m) * 1e2 - (bound_sep_cm - lim_sep_cm), c="m")


def plot_overview(res):
    """Plot an overview of a KN1D run, showing both kinetic profile inputs and
    a small selection of the outputs.

    Parameters
    ----------
    res : dict
        Output dictionary from function :py:func:`~aurora.kn1d.run_kn1d`.
    """

    ins = res["kn1d_input"]
    outs = res["out"]

    fig, ax = plt.subplots(4, 1, sharex=True, figsize=(10, 10))

    mu = int(ins["mu"])
    species = "H" if mu == 1 else "D"

    (line,) = ax[0].plot(ins["x"], ins["n"] / 1e19, lw=2.0)

    c = line.get_color()

    ax[1].semilogy(ins["x"], ins["te"], lw=2.0, c=c, ls="-", label=r"$T_e$")
    ax[1].semilogy(ins["x"], ins["ti"], lw=2.0, c=c, ls="--", label=r"$T_i$")
    ax[1].semilogy(
        outs["xh"], outs["th"], lw=2.0, c=c, ls="-.", label=rf"$T_{species}$"
    )

    ax[2].semilogy(outs["xh"], outs["nh"], lw=2.0, c=c, ls="-", label=rf"$n_{species}$")
    ax[2].semilogy(
        outs["xh2"], outs["nh2"], lw=2.0, c=c, ls="--", label=r"$n_{%s2}$" % species
    )
    ax[2].semilogy(
        outs["xh2"], outs["nhp"], lw=2.0, c=c, ls="-.", label=r"$n_{%s2}^+$" % species
    )

    # quasineutrality in a pure plasma: nH+ = ne - nH2+ (NB: NH2+ is saved as nhp)
    nhp_interp = interp1d(outs["xh2"], outs["nhp"], bounds_error=False, fill_value=0.0)(
        ins["x"]
    )
    ax[2].semilogy(
        ins["x"],
        ins["n"] - nhp_interp,
        lw=2.0,
        c=c,
        ls=":",
        label=r"$n_e - n_{%s2}^+$" % species,
    )

    ax[3].semilogy(
        outs["xh"], outs["sion"] / 1e20, lw=2.0, c=c, label="Atomic ionization rate"
    )

    # annotate location of limiter and LCFS
    ax[0].axvline(ins["xlimiter"])
    ax[0].axvline(ins["xsep"])
    ax[1].axvline(ins["xlimiter"])
    ax[1].axvline(ins["xsep"])
    ax[2].axvline(ins["xlimiter"])
    ax[2].axvline(ins["xsep"])
    ax[3].axvline(ins["xlimiter"])
    ax[3].axvline(ins["xsep"])

    dist = ins["xlimiter"] / 10.0  # convenient rule-of-thumb for plotting
    ax[0].annotate(
        "Limiter",
        (ins["xlimiter"] + dist, 0.5 * ax[0].get_ylim()[1]),
        fontsize=14,
        rotation=90,
    )
    ax[0].annotate(
        "LCFS",
        (ins["xsep"] + dist, 0.5 * ax[0].get_ylim()[1]),
        fontsize=14,
        rotation=90,
    )

    # reduce number of ticks from default
    for n_ax in [0, 1, 2, 3]:
        nyticks = len(ax[n_ax].get_yticks())
        ax[n_ax].set_yticks(ax[n_ax].get_yticks()[::2])

    # set every other ylabel/ticks to the right
    ax[1].yaxis.set_label_position("right")
    ax[1].yaxis.tick_right()
    ax[3].yaxis.set_label_position("right")
    ax[3].yaxis.tick_right()

    ax[0].set_ylabel(r"$n_e$ [$10^{19}$ $m^{-3}$]")
    ax[1].set_ylabel(r"$eV$")
    ax[2].set_ylabel(r"$m^{-3}$")
    ax[3].set_ylabel(r"$10^{20}$ $m^{-3}$")
    ax[-1].set_xlabel("Distance from the wall [m]")

    # legends
    # ax[0].legend(fontsize=14, loc='best')
    ax[1].legend(fontsize=14, loc="best")
    ax[2].legend(fontsize=14, loc="best")
    ax[3].legend(fontsize=14, loc="best")


def plot_exc_states(res):
    """Plot excited state fractions of atomic neutral density from a KN1D run.

    Parameters
    ----------
    res : dict
        Output dictionary from function :py:func:`~aurora.kn1d.run_kn1d`.
    """
    ins = res["kn1d_input"]
    outs = res["out"]

    fig, ax = plt.subplots(4, 1, sharex=True, figsize=(10, 10))

    _ne = interp1d(ins["x"], ins["n"], bounds_error=False, fill_value="extrapolate")(
        outs["xh"]
    )
    (line,) = ax[0].semilogy(outs["xh"], outs["nh"] / _ne, lw=2.0)
    c = line.get_color()

    ax[1].plot(outs["xh"], outs["nh"], c=c, lw=2.0)

    ax[2].plot(ins["x"], outs["N2"], c=c, lw=2.0, ls="-", label="total")
    ax[2].plot(ins["x"], outs["N2_ground"], c=c, lw=2.0, ls="--", label="from ground")
    ax[2].plot(ins["x"], outs["N2_cont"], c=c, lw=2.0, ls="-.", label="from cont.")

    ax[3].plot(ins["x"], outs["N3"], c=c, ls="-", lw=2.0, label="total")
    ax[3].plot(ins["x"], outs["N3_ground"], c=c, lw=2.0, ls="--", label="from ground")
    ax[3].plot(ins["x"], outs["N3_cont"], c=c, lw=2.0, ls="-.", label="from cont.")

    # annotate location of limiter and LCFS
    ax[0].axvline(ins["xlimiter"])
    ax[0].axvline(ins["xsep"])
    ax[1].axvline(ins["xlimiter"])
    ax[1].axvline(ins["xsep"])
    ax[2].axvline(ins["xlimiter"])
    ax[2].axvline(ins["xsep"])
    ax[3].axvline(ins["xlimiter"])
    ax[3].axvline(ins["xsep"])

    dist = ins["xlimiter"] / 10.0  # convenient rule-of-thumb for plotting
    ax[0].annotate(
        "Limiter",
        (ins["xlimiter"] + dist, 1e-3 * ax[0].get_ylim()[1]),
        fontsize=14,
        rotation=90,
    )
    ax[0].annotate(
        "LCFS",
        (ins["xsep"] + dist, 1e-3 * ax[0].get_ylim()[1]),
        fontsize=14,
        rotation=90,
    )

    ax[0].set_ylabel(r"$n_{n,1}/n_e$")
    # ax[0].legend(loc='best')

    ax[1].set_ylabel(r"$n_{n,1}$ [m$^{-3}$]")

    ax[2].set_ylabel(r"$n_{n,2}$ [m$^{-3}$]")
    ax[2].legend(loc="best")

    ax[3].set_ylabel(r"$n_{n,3}$ [m$^{-3}$]")
    ax[3].legend(loc="best")

    ax[-1].set_xlabel("Distance from the wall [m]")


def plot_emiss(res, check_collrad=True):
    """Plot profiles of Ly-a and D-alpha emissivity from the KN1D output.
    KN1D internally computes Ly-a and D-alpha emission using the Johnson-Hinnov
    coefficients; here we check the result of that calculation and compare it to the
    prediction from atomic data from the COLLRAD collisional-radiative model included
    in DEGAS2.

    Parameters
    ----------
    res : dict
        Output dictionary from function :py:func:`~aurora.kn1d.run_kn1d`.
    check_collrad : bool
        If True, compare KN1D prediction of Ly-a and D-a emission using Johnson-Hinnov
        rates using rates from COLLRAD.
    """

    ins = res["kn1d_input"]
    outs = res["out"]

    mu = int(ins["mu"])
    fig, ax = plt.subplots(2, 1, sharex=True, figsize=(10, 8))
    (line,) = ax[0].plot(outs["xh"], outs["lyman"], ls="-")
    c = line.get_color()

    ax[1].plot(outs["xh"], outs["balmer"], "-", c=c, label="KN1D (JH)")

    # annotate location of limiter and LCFS
    ax[0].axvline(ins["xlimiter"])
    ax[0].axvline(ins["xsep"])
    ax[1].axvline(ins["xlimiter"])
    ax[1].axvline(ins["xsep"])

    dist = ins["xlimiter"] / 10.0  # convenient rule-of-thumb for plotting
    ax[0].annotate(
        "Limiter",
        (ins["xlimiter"] + dist, 0.5 * ax[0].get_ylim()[1]),
        fontsize=14,
        rotation=90,
    )
    ax[0].annotate(
        "LCFS",
        (ins["xsep"] + dist, 0.5 * ax[0].get_ylim()[1]),
        fontsize=14,
        rotation=90,
    )

    ax[-1].set_xlabel("Distance from the wall [m]")
    # ---------------
    if check_collrad:
        # test KN1D calculation of Ly-a and D-a

        thirteenpointsix = h * c_light * Rydberg / e
        E_32 = thirteenpointsix * (2.0 ** (-2.0) - 3.0 ** (-2.0)) * e  # J
        E_21 = thirteenpointsix * (1.0 - 2.0 ** (-2.0)) * e  # J

        # Balmer wavelengths from DEGAS2:
        # \lambda_{H_\alpha} ~ 6562.80 A
        # \lambda_{D_\alpha} ~ 6561.04 A
        # \lambda_{T_\alpha} ~ 6560.45 A

        # Lyman series spontaneous emission coeffs for n=2 to 1, 3 to 1, ... 16 to 1
        A_lyman = [
            4.699e8,
            5.575e7,
            1.278e7,
            4.125e6,
            1.644e6,
            7.568e5,
            3.869e5,
            2.143e5,
            1.263e5,
            7.834e4,
            5.066e4,
            3.393e4,
            2.341e4,
            1.657e4,
            1.200e4,
        ]

        # Balmer series spontaneous emission coeffs for n=3 to 2, 4 to 2, ... 17 to 2
        A_balmer = [
            4.41e7,
            8.42e6,
            2.53e6,
            9.732e5,
            4.389e5,
            2.215e5,
            1.216e5,
            7.122e4,
            4.397e4,
            2.83e4,
            18288.8,
            12249.1,
            8451.26,
            5981.95,
            4332.13,
        ]

        A_21 = A_lyman[0]  # Ly-alpha
        A_32 = A_balmer[0]  # D-alpha

        Ly_alpha = interp1d(
            ins["x"], E_21 * outs["N2"] * A_21, kind="linear", bounds_error=False
        )(
            outs["xh"]
        )  # J/s/m^3
        D_alpha = interp1d(
            ins["x"], E_32 * outs["N3"] * A_32, kind="linear", bounds_error=False
        )(
            outs["xh"]
        )  # J/s/m^3

        ax[0].plot(outs["xh"], Ly_alpha, "--", c=c)
        ax[1].plot(outs["xh"], D_alpha, "--", c=c, label="collrad")

        ax[0].set_ylabel(r"Ly-alpha [W m$^{-3}$]")
        ax[1].set_ylabel(
            rf'{"H" if mu == 1 else "D"}-alpha [W m$^{-3}$]'
        )  # 656.28 nm in air
        ax[-1].set_xlabel("Distance from the wall [m]")

        ax[0].legend(loc="best")
        ax[1].legend(loc="best")


def plot_transport(res):
    """Make a simple set of plots of gradient scale lengths and effective diffusion coefficients
    from the KN1D output.

    Parameters
    ----------
    res : dict
        Output dictionary from function :py:func:`~aurora.kn1d.run_kn1d`.
    """
    ins = res["kn1d_input"]
    outs = res["out"]

    fig, ax = plt.subplots(3, 1, sharex=True, figsize=(10, 7.5))

    (line,) = ax[0].plot(ins["x"], outs["L_ne"], lw=2.0, label=r"$L_{n_e}$")
    c = line.get_color()

    ax[0].plot(ins["x"], outs["L_Te"], lw=2.0, c=c, ls="--", label=r"$L_{T_e}$")
    ax[0].plot(ins["x"], outs["L_Ti"], lw=2.0, c=c, ls="-.", label=r"$L_{T_i}$")

    # Effective diffusivity from Gamma_i/\nabla(n_e)
    ax[1].plot(ins["x"], outs["D_eff"], lw=2.0, c=c)

    # atomic and ion source profiles
    ax[2].plot(outs["xH2"], outs["sh"], lw=2.0, c=c, ls="-", label="Atomic source")
    ax[2].plot(outs["xH2"], outs["sp"], lw=2.0, c=c, ls="--", label="Ion source")

    # annotate location of limiter and LCFS
    ax[0].axvline(ins["xlimiter"])
    ax[0].axvline(ins["xsep"])
    ax[1].axvline(ins["xlimiter"])
    ax[1].axvline(ins["xsep"])
    ax[2].axvline(ins["xlimiter"])
    ax[2].axvline(ins["xsep"])

    dist = ins["xlimiter"] / 10.0  # convenient rule-of-thumb for plotting
    ax[0].annotate(
        "Limiter",
        (ins["xlimiter"] + dist, 0.5 * ax[0].get_ylim()[1]),
        fontsize=14,
        rotation=90,
    )
    ax[0].annotate(
        "LCFS",
        (ins["xsep"] + dist, 0.5 * ax[0].get_ylim()[1]),
        fontsize=14,
        rotation=90,
    )

    ax[0].legend(fontsize=16, loc="best")

    ax[1].set_ylabel(r"$D_{eff}$ [$m^2$ $s^{-1}$]", fontsize=16)
    # ax[1].legend(fontsize=16, loc='best')

    ax[2].set_ylabel(r"$m^{-3}$ $s^{-1}$", fontsize=16)
    ax[2].legend(fontsize=16, loc="best")

    ax[-1].set_xlabel("Distance from the wall [m]")
