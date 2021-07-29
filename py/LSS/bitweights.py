"""
Functions for calculating PIP weights
"""
import os
import h5py
import numpy as np
from astropy.table import Table
from fiberassign.targets import (Targets, TargetsAvailable, TargetTree,
                                 LocationsAvailable, load_target_table)
from fiberassign.assign import Assignment


def get_targets(mtlfile, skyfile):
    """
    Load target and information
    """
    # Read mtl file
    mtl = Table.read(mtlfile)
    if 'SUBPRIORITY' not in mtl:
        mtl['SUBPRIORITY'] = np.ones(len(mtl))
    if 'OBSCONDITIONS' not in mtl:
        mtl['OBSCONDITIONS'] = np.ones(len(mtl), dtype=int)

    # Load science targets
    tgs = Targets()
    load_target_table(tgs, mtl)

    # Load sky targets
    sky = None
    if skyfile:
        sky = Table.read(skyfile)
        load_target_table(tgs, sky)

    return mtl, tgs, sky

def setup_fba(mtl, sky, tiles, hw):
    """
    Set up tiles, targets, etc. for fiber assignment
    """
    # Load targets and target tree
    tgs = Targets()
    load_target_table(tgs, mtl)
    if sky:
        load_target_table(tgs, sky)
    tree = TargetTree(tgs)

    # Compute available targets / locations
    tgsavail = TargetsAvailable(hw, tgs, tiles, tree)
    favail = LocationsAvailable(tgsavail)
    asgn = Assignment(tgs, tgsavail, favail)

    return asgn

def update_bitweights(realization, asgn, tile_id, tg_ids, tg_ids2idx, bitweights):
    """
    Update bit weights for assigned science targets
    """
    adata = asgn.tile_location_target(tile_id)
    for loc, tgid in adata.items():
        try: # Find which targets were assigned
            idx = tg_ids2idx[tgid]
            bitweights[realization * len(tg_ids) + idx] = True
        except:
            pass

    return bitweights

def pack_bitweights(array):
    """
    Creates an array of bitwise weights stored as 64-bit signed integers
    Input: a 2D boolean array of shape (Ngal, Nreal), where Ngal is the total number 
           of target galaxies, and Nreal is the number of fibre assignment realizations.
    Output: returns a 2D array of 64-bit signed integers. 
    """
    Nbits = 64
    dtype = np.int64
    Ngal, Nreal = array.shape           # total number of realizations and number of target galaxies
    Nout = (Nreal + Nbits - 1) // Nbits # number of output columns
    # intermediate arrays
    bitw8 = np.zeros((Ngal, 8), dtype="i")   # array of individual bits of 8 realizations
    bitweights = np.zeros(Ngal, dtype=dtype) # array of 64-bit integers
    # array to store final output
    output_array = np.zeros((Ngal, Nout), dtype=dtype)
    idx_out = 0 # initial column in output_array
    # loop through realizations to build bitwise weights
    for i in range(Nreal):
        bitw8[array[:,i], i%8] = 1
        arr = np.array(np.packbits(bitw8[:,::-1]), dtype=dtype)
        bitweights = np.bitwise_or(bitweights, np.left_shift(arr, 8*((i%Nbits)//8)))
        if (i+1)%Nbits == 0 or i+1 == Nreal:
            output_array[:,idx_out] = bitweights
            bitweights[:] = 0
            idx_out += 1
        if (i+1)%8 == 0:
            bitw8[:] = 0
    return output_array

def write_output(outdir, fileformat, targets, bitvectors, idas, desi_target_key=None):
    """
    Write output file containing bit weights
    """
    try:
        os.makedirs(outdir)
    except:
        pass

    # Output fits files
    if fileformat == 'fits':
        outfile = os.path.join(outdir, '{}.fits'.format(outtype))
        output = Table()
        output['TARGETID'] = targets['TARGETID']
        if desi_target_key:
            output['{}'.format(desi_target_key)] = targets['{}'.format(desi_target_key)]
        output['RA'] = targets['RA']
        output['DEC'] = targets['DEC']
        output['Z'] = targets['Z']
        for i in range(bitvectors.shape[1]):
            output['BITWEIGHT{}'.format(i)] = bitvectors[:,i][idas]
        output.write(outfile)

    # Output hdf5 files
    elif fileformat == 'hdf5':
        outfile = os.path.join(outdir, '{}.hdf5'.format(outtype))
        outfile = h5py.File(outfile, 'w')
        if outtype == 'targeted' or outtype == 'parent':
            outfile.create_dataset('TARGETID', data=targets['TARGETID'])
            if desi_target_key:
                outfile.create_dataset('{}'.format(desi_target_key), data=targets['{}'.format(desi_target_key)])
        outfile.create_dataset('RA', data=targets['RA'])
        outfile.create_dataset('DEC', data=targets['DEC'])
        outfile.create_dataset('Z', data=targets['Z'])
        for i in range(bitvectors.shape[1]):
            outfile.create_dataset('BITWEIGHT{}'.format(i), data=bitvectors[:,i][idas])
        outfile.close()

    else:
        raise TypeError("Must specify either fits or hdf5 as output file format.")

    return

