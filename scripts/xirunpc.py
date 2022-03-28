import os
import argparse
import logging
import numpy as np
from astropy.table import Table, vstack
from matplotlib import pyplot as plt

from pycorr import TwoPointCorrelationFunction, TwoPointEstimator, KMeansSubsampler, utils, setup_logging
from LSS.tabulated_cosmo import TabulatedDESI


logger = logging.getLogger('xirunpc')


def get_zlims(tracer, tracer2=None, option=None):
    
    if tracer2 is not None:
        zlims1 = get_zlims(tracer, option=option)
        zlims2 = get_zlims(tracer2, option=option)
        return [zlim for zlim in zlims1 if zlim in zlims2]

    if tracer.startswith('LRG'):
        zlims = [0.4, 0.6, 0.8, 1.1]

    if tracer.startswith('ELG'):# or type == 'ELG_HIP':
        zlims = [0.8, 1.1, 1.5]
        if option == 'safez':
            zlims = [0.9, 1.48]

    if tracer.startswith('QSO'):
        zlims = [0.8, 1.1, 1.5, 2.1]
        if option == 'highz':
            zlims = [2.1, 3.5]

    if tracer.startswith('BGS'):
        zlims = [0.1, 0.3, 0.5]
        if option == 'lowz':
            zlims = [0.1, 0.3]
        if option == 'highz':
            zlims = [0.3, 0.5]
    
    if option == 'fullonly':
        zlims = [zlims[0], zlims[-1]]

    return zlims


def select_region(ra, dec, reg):
    wra = (ra > 100 - dec)
    wra &= (ra < 280 + dec)
    if reg == 'DN':
        w = dec < 32.375
        w &= wra
    if reg == 'DS':
        w = ~wra
        w &= dec > -25
    return w


def catalog_dir(survey='main', verspec='guadalupe', version='test', base_dir='/global/cfs/cdirs/desi/survey/catalogs'):
    return os.path.join(base_dir, survey, 'LSS', verspec, 'LSScats', version)


def catalog_fn(tracer='ELG', region='', ctype='clustering', name='data', rec_type=False, nrandoms=4, cat_dir=None, survey='main', **kwargs):
    if cat_dir is None:
        cat_dir = catalog_dir(survey=survey, **kwargs)
    if ctype == 'clustering' and survey in ['main', 'DA02']:
        tracer += 'zdone'
    dat_or_ran = name[:3]
    if region: region = '_' + region
    if rec_type:
        return os.path.join(cat_dir, '{}{}_{}_{}{}.{}.fits'.format(tracer, region, ctype, rec_type, dat_or_ran, rec_conv))
    if name == 'data':
        return os.path.join(cat_dir, '{}{}_{}.{}.fits'.format(tracer, region, ctype, dat_or_ran))
    return [os.path.join(cat_dir, '{}{}_{:d}_{}.{}.fits'.format(tracer, region, iran, ctype, dat_or_ran)) for iran in range(nrandoms)]


def read_clustering_positions_weights(distance, zlim=(0., np.inf), weight_type='default', name='data', **kwargs):
    
    cat_fn = catalog_fn(ctype='clustering', name=name, **kwargs)
    logger.info('Loading {}.'.format(cat_fn))
    if isinstance(cat_fn, (tuple, list)):
        catalog = vstack([Table.read(fn) for fn in cat_fn])
    else:
        catalog = Table.read(cat_fn)

    mask = (catalog['Z'] >= zlim[0]) & (catalog['Z'] < zlim[1])
    logger.info('Using {:d} rows for {}.'.format(mask.sum(), name))
    positions = [catalog['RA'][mask], catalog['DEC'][mask], distance(catalog['Z'][mask])]
    weights = np.ones_like(positions[0])
    
    if 'completeness_only' in weight_type and 'bitwise' in weight_type:
        raise ValueError('inconsistent choices were put into weight_type')

    if name == 'data':
        if 'zfail' in weight_type:
            weights *= catalog['WEIGHT_ZFAIL'][mask]
        if 'default' in weight_type and 'bitwise' not in weight_type:
            weights *= catalog['WEIGHT'][mask]
        if 'RF' in weight_type:
            weights *= catalog['WEIGHT_RF'][mask]*catalog['WEIGHT_COMP'][mask]
        if 'completeness_only' in weight_type:
            weights = catalog['WEIGHT_COMP'][mask]
        if 'FKP' in weight_type:
            weights *= catalog['WEIGHT_FKP'][mask]
        if 'bitwise' in weight_type:
            weights = list(catalog['BITWEIGHTS'][mask].T) + [weights]
    
    if name == 'randoms':
        if 'default' in weight_type:
            weights *= catalog['WEIGHT'][mask]
        if 'RF' in weight_type:
            weights *= catalog['WEIGHT_RF'][mask]*catalog['WEIGHT_COMP'][mask]
        if 'zfail' in weight_type:
            weights *= catalog['WEIGHT_ZFAIL'][mask]
        if 'completeness_only' in weight_type:
            weights = catalog['WEIGHT_COMP'][mask]
        if 'FKP' in weight_type:
            weights *= catalog['WEIGHT_FKP'][mask]

    return positions, weights


def read_full_positions_weights(name='data', weight_type='default', fibered=False, **kwargs):
    
    cat_fn = catalog_fn(ctype='full', name=name, **kwargs)
    if isinstance(cat_fn, (tuple, list)):
        catalog = vstack([Table.read(fn) for fn in cat_fn])
    else:
        catalog = Table.read(cat_fn)
    
    mask = np.ones(len(catalog), dtype='?')
    if region in ['DS', 'DN']:
        mask &= select_region(catalog['RA'], catalog['DEC'], region)
    elif region:
        mask &= catalog['PHOTSYS'] == region.strip('_')

    if fibered: mask &= catalog['LOCATION_ASSIGNED']
    positions = [catalog['RA'][mask], catalog['DEC'][mask], catalog['DEC'][mask]]
    if fibered and 'bitwise' in weight_type: weights = list(catalog['BITWEIGHTS'][mask].T)
    else: weights = np.ones_like(positions[0])
    return positions, weights


def compute_correlation_function(corr_type, edges, distance, nthreads=8, dtype='f8', wang=None, weight_type='default', tracer='ELG', tracer2=None, njack=120, mpicomm=None, mpiroot=None, rec_type=None, **kwargs):
    
    autocorr = tracer2 is None
    catalog_kwargs = kwargs.copy()
    catalog_kwargs['weight_type'] = weight_type
    with_shifted = rec_type is not None
    
    if 'angular' in weight_type and wang is None:
        
        fibered_data_positions1, fibered_data_weights1, fibered_data_positions2, fibered_data_weights2 = None, None, None, None
        parent_data_positions1, parent_data_weights1, parent_data_positions2, parent_data_weights2 = None, None, None, None
        parent_randoms_positions1, parent_randoms_weights1, parent_randoms_positions2, parent_randoms_weights2 = None, None, None, None
        
        if mpicomm is None or mpicomm.rank == mpiroot:

            catalog_kwargs = kwargs.copy()
            catalog_kwargs['weight_type'] = weight_type
            
            fibered_data_positions1, fibered_data_weights1 = read_full_positions_weights(name='data', fibered=False, tracer=tracer, **catalog_kwargs)
            parent_data_positions1, parent_data_weights1 = read_full_positions_weights(name='data', fibered=True, tracer=tracer, **catalog_kwargs)
            parent_randoms_positions1, parent_randoms_weights1 = read_full_positions_weights(name='randoms', tracer=tracer, **catalog_kwargs)
            if not autocorr:
                fibered_data_positions2, fibered_data_weights2 = read_full_positions_weights(name='data', fibered=False, tracer=tracer2, **catalog_kwargs)
                parent_data_positions2, parent_data_weights2 = read_full_positions_weights(name='data', fibered=True, tracer=tracer2, **catalog_kwargs)
                parent_randoms_positions2, parent_randoms_weights2 = read_full_positions_weights(name='randoms', tracer=tracer2, **catalog_kwargs)
        
        tedges = np.logspace(-4., 0.5, 41)
        # First D1D2_parent/D1D2_PIP angular weight
        wangD1D2 = TwoPointCorrelationFunction('theta', tedges, data_positions1=fibered_data_positions1, data_weights1=fibered_data_weights1,
                                               data_positions2=fibered_data_positions2, data_weights2=fibered_data_weights2,
                                               randoms_positions1=parent_data_positions1, randoms_weights1=parent_data_weights1,
                                               randoms_positions2=parent_data_positions2, randoms_weights2=parent_data_weights2,
                                               estimator='weight', engine='corrfunc', position_type='rdd', nthreads=nthreads,
                                               dtype=dtype, mpicomm=mpicomm, mpiroot=mpiroot)

        # First D1R2_parent/D1R2_IIP angular weight
        # Input bitwise weights are automatically turned into IIP
        if autocorr:
            parent_randoms_positions2, parent_randoms_weights2 = parent_randoms_positions1, parent_randoms_weights1
        wangD1R2 = TwoPointCorrelationFunction('theta', tedges, data_positions1=fibered_data_positions1, data_weights1=fibered_data_weights1,
                                               data_positions2=parent_randoms_positions2, data_weights2=parent_randoms_weights2,
                                               randoms_positions1=parent_data_positions1, randoms_weights1=parent_data_weights1,
                                               randoms_positions2=parent_randoms_positions2, randoms_weights2=parent_randoms_weights2,
                                               estimator='weight', engine='corrfunc', position_type='rdd', nthreads=nthreads,
                                               dtype=dtype, mpicomm=mpicomm, mpiroot=mpiroot)
        wangR1D2 = None
        if not autocorr:
            wangR1D2 = TwoPointCorrelationFunction('theta', tedges, data_positions1=parent_randoms_positions1, data_weights1=parent_randoms_weights1,
                                                   data_positions2=fibered_data_positions2, data_weights2=fibered_data_weights2,
                                                   randoms_positions1=parent_randoms_positions1, randoms_weights1=parent_randpms_weights1,
                                                   randoms_positions2=parent_data_positions2, randoms_weights2=parent_data_weights2,
                                                   estimator='weight', engine='corrfunc', position_type='rdd', nthreads=nthreads,
                                                   dtype=dtype, mpicomm=mpicomm, mpiroot=mpiroot)
        wang = {}
        wang['D1D2_twopoint_weights'] = wangD1D2
        wang['D1R2_twopoint_weights'] = wangD1R2
        wang['R1D2_twopoint_weights'] = wangR1D2
    
    data_positions1, data_weights1, data_samples1, data_positions2, data_weights2, data_samples2 = None, None, None, None, None, None
    randoms_positions1, randoms_weights1, randoms_samples1, randoms_positions2, randoms_weights2, randoms_samples2 = None, None, None, None, None, None
    shifted_positions1, shifted_weights1, shifted_samples1, shifted_positions2, shifted_weights2, shifted_samples2 = None, None, None, None, None, None
    
    if mpicomm is None or mpicomm.rank == mpiroot:
        
        if with_shifted:
            data_positions1, data_weights1 = read_clustering_positions_weights(distance, name='data', rec_type=rec_type, tracer=tracer, **catalog_kwargs)
            shifted_positions1, shifted_weights1 = read_clustering_positions_weights(distance, name='randoms', rec_type=rec_type, tracer=tracer, **catalog_kwargs)
        else:
            data_positions1, data_weights1 = read_clustering_positions_weights(distance, name='data', rec_type=rec_type, tracer=tracer, **catalog_kwargs)
        randoms_positions1, randoms_weights1 = read_clustering_positions_weights(distance, name='randoms', rec_type=rec_type, tracer=tracer, **catalog_kwargs)
        jack_positions = data_positions1
        
        if not autocorr:
            if with_shifted:
                data_positions2, data_weights2 = read_clustering_positions_weights(distance, name='data', rec_type=rec_type, tracer=tracer2, **catalog_kwargs)
                shifted_positions2, shifted_weights2 = read_clustering_positions_weights(distance, name='randoms', rec_type=rec_type, tracer=tracer2, **catalog_kwargs)
            else:
                data_positions2, data_weights2 = read_clustering_positions_weights(distance, name='data', rec_type=rec_type, tracer=tracer2, **catalog_kwargs)
            randoms_positions2, randoms_weights2 = read_clustering_positions_weights(distance, name='randoms', rec_type=rec_type, tracer=tracer2, **catalog_kwargs)
            jack_positions = [np.concatenate([p1, p2], axis=0) for p1, p2 in zip(jack_positions, data_positions2)]
    
    if njack >= 2:
        subsampler = KMeansSubsampler('angular', positions=jack_positions, nsamples=njack, nside=512, random_state=42, position_type='rdd',
                                      dtype=dtype, mpicomm=mpicomm, mpiroot=mpiroot)

        if mpicomm is None or mpicomm.rank == mpiroot:
            data_samples1 = subsampler.label(data_positions1)
            randoms_samples1 = subsampler.label(randoms_positions1)
            if with_shifted:
                shifted_samples1 = subsampler.label(shifted_positions1)
            if not autocorr:
                data_samples2 = subsampler.label(data_positions2)
                randoms_samples2 = subsampler.label(randoms_positions2)
                if with_shifted:
                    shifted_samples2 = subsampler.label(shifted_positions2)

    kwargs = {}
    kwargs.update(wang or {})

    result = TwoPointCorrelationFunction(corr_type, edges, data_positions1=data_positions1, data_weights1=data_weights1, data_samples1=data_samples1,
                                         data_positions2=data_positions2, data_weights2=data_weights2, data_samples2=data_samples2,
                                         randoms_positions1=randoms_positions1, randoms_weights1=randoms_weights1, randoms_samples1=randoms_samples1,
                                         randoms_positions2=randoms_positions2, randoms_weights2=randoms_weights2, randoms_samples2=randoms_samples2,
                                         shifted_positions1=shifted_positions1, shifted_weights1=shifted_weights1, shifted_samples1=shifted_samples1,
                                         shifted_positions2=shifted_positions2, shifted_weights2=shifted_weights2, shifted_samples2=shifted_samples2,
                                         engine='corrfunc', position_type='rdd', nthreads=nthreads, dtype=dtype, mpicomm=mpicomm, mpiroot=mpiroot, **kwargs)
    return result, wang


def get_regions(survey, rec=False):
    regions = ['N', 'S', '']
    if survey in ['main', 'DA02']:
        regions = ['DN', 'DS', 'N', 'S', '']
        if rec: regions = ['DN', 'N']
    return regions


def get_edges(corr_type='smu', bin_type='lin'):
    
    if bin_type == 'log':
        sedges = np.geomspace(0.1, 30., 16)
    if bin_type == 'lin':
        sedges = np.linspace(0., 200, 201)
    if corr_type == 'smu':
        edges = (sedges, np.linspace(-1., 1., 201)) #s is input edges and mu evenly spaced between 0 and 1
    if corr_type == 'rppi':
        if bin_type == 'lin':
            edges = (sedges, sedges) #transverse and radial separations are coded to be the same here
        else:
            edges = (sedges, np.linspace(0., 40., 41))
    if corr_type == 'theta':
        edges = np.linspace(0., 4., 101)
    return edges


def corr_fn(file_type='npy', region='', tracer='ELG', tracer2=None, zmin=0, zmax=np.inf, rec_type=False, weight_type='default', bin_type='lin', out_dir='.'):
    if tracer2: tracer += '_' + tracer2
    if rec_type: tracer += '_' + rec_type
    if region: tracer += '_' + region
    root = '{}_{}_{}_{}_{}'.format(tracer, zmin, zmax, weight_type, bin_type)
    if file_type == 'npy':
        return os.path.join(out_dir, 'allcounts_{}.npy'.format(root))
    return os.path.join(out_dir, '{}_{}.txt'.format(file_type, root))


if __name__ == '__main__':
    
    parser = argparse.ArgumentParser()
    parser.add_argument('--tracer', help='tracer(s) to be selected - 2 for cross-correlation', type=str, nargs='+', default=['ELG'])
    parser.add_argument('--basedir', help='where to find catalogs', type=str, default='/global/cfs/cdirs/desi/survey/catalogs')
    parser.add_argument('--survey', help='e.g., SV3 or main', type=str, choices=['SV3', 'DA02', 'main'], default='SV3')
    parser.add_argument('--verspec', help='version for redshifts', type=str, default='everest')
    parser.add_argument('--version', help='catalog version', type=str, default='test')
    parser.add_argument('--nran', help='number of random files to combine together (1-18 available)', type=int, default=4)
    parser.add_argument('--region', help='regions; by default, run on all regions', type=str, nargs='*', choices=['N', 'S', 'DN', 'DS', ''], default=None)
    parser.add_argument('--corr_type', help='correlation type', type=str, nargs='*', choices=['smu', 'rppi', 'theta'], default=['smu', 'rppi'])
    parser.add_argument('--weight_type', help='types of weights to use; use default_angular_bitwise for PIP with angular upweighting; default just uses WEIGHT column', type=str, default='default')
    parser.add_argument('--bin_type', help='binning type', type=str, choices=['log', 'lin'], default='lin')
    parser.add_argument('--njack', help='number of jack-knife subsamples; 0 for no jack-knife error estimates', type=int, default=120)
    parser.add_argument('--nthreads', help='number of threads', type=int, default=64)
    parser.add_argument('--zlims', help='z-limits, or options for z-limits, e.g. "highz", "lowz", "fullonly"', type=str, nargs='*', default=None)
    parser.add_argument('--outdir', help='base directory for output', type=str, default=None)
    #parser.add_argument('--mpi', help='whether to use MPI', action='store_true', default=False)
    parser.add_argument('--vis', help='show plot of each xi?', action='store_true', default=False)

    #only relevant for reconstruction
    parser.add_argument('--rec_type', help='reconstruction algorithm + reconstruction convention', choices=['IFTrecsym', 'IFTreciso', 'MGrecsym', 'MGreciso'], type=str, default=None)

    setup_logging()
    args = parser.parse_args()
    
    mpicomm, mpiroot = None, None
    if True:#args.mpi:
        from pycorr import mpi
        mpicomm = mpi.COMM_WORLD
        mpiroot = 0

    cat_dir = catalog_dir(base_dir=args.basedir, survey=args.survey, verspec=args.verspec, version=args.version)
    out_dir = os.path.join(os.environ['CSCRATCH'], args.survey)
    if args.outdir is not None: out_dir = args.outdir
    tracer, tracer2 = args.tracer[0], None
    if len(args.tracer) > 1:
        tracer2 = args.tracer[1]
        if len(args.tracer) > 2:
            raise ValueError('Provide <= 2 tracers!')
    if tracer2 == tracer:
        tracer2 = None # otherwise counting of self-pairs
    catalog_kwargs = dict(tracer=tracer, tracer2=tracer2, survey=args.survey, cat_dir=cat_dir, rec_type=args.rec_type) # survey required for zdone
    distance = TabulatedDESI().comoving_radial_distance
    
    if args.zlims is None:
        zlims = get_zlims(tracer, tracer2=tracer2)
    elif not args.zlims[0].replace('.', '').isdigit():
        zlims = get_zlims(tracer, tracer2=tracer2, option=args.zlims[0])
    else:
        zlims = [float(zlim) for zlim in args.zlims]
    zlims = list(zip(zlims[:-1], zlims[1:])) + [(zlims[0], zlims[-1])]
    regions = args.region
    if regions is None:
        regions = get_regions(args.survey, rec=bool(args.rec_type))

    rebinning_factors = [1, 4, 5, 10] if 'lin' in args.bin_type else [1]
    if mpicomm is None or mpicomm.rank == mpiroot:
        logger.info('Computing correlation function {} in regions {} in redshift ranges {}.'.format(args.corr_type, regions, zlims))
    
    for zmin, zmax in zlims:
        for region in regions:
            wang = None
            for corr_type in args.corr_type:
                if mpicomm is None or mpicomm.rank == mpiroot:
                    logger.info('Computing correlation function {} in region {} in redshift range {}.'.format(corr_type, region, (zmin, zmax)))
                edges = get_edges(corr_type=corr_type, bin_type=args.bin_type)
                result, wang = compute_correlation_function(corr_type, edges=edges, distance=distance, nrandoms=args.nran, nthreads=args.nthreads, zlim=(zmin, zmax), region=region, weight_type=args.weight_type, njack=args.njack, wang=wang, mpicomm=mpicomm, mpiroot=mpiroot, **catalog_kwargs)
                #save pair counts
                fn = corr_fn(file_type='npy', region=region, tracer=tracer, tracer2=tracer2, zmin=zmin, zmax=zmax, rec_type=args.rec_type, weight_type=args.weight_type, bin_type=args.bin_type, out_dir=out_dir)
                result.save(fn)
                for factor in rebinning_factors:
                    #result = TwoPointEstimator.load(fn)
                    rebinned = result[:(result.shape[0]//factor)*factor:factor]
                    txt_kwargs = dict(region=region, tracer=tracer, tracer2=tracer2, zmin=zmin, zmax=zmax, rec_type=args.rec_type, weight_type=args.weight_type, bin_type=args.bin_type+str(factor), out_dir=out_dir)
                    if corr_type == 'smu':
                        fn_txt = corr_fn(file_type='xismu', **txt_kwargs)
                        rebinned.save_txt(fn_txt)
                        fn_txt = corr_fn(file_type='xipoles', **txt_kwargs)
                        rebinned.save_txt(fn_txt, ells=(0, 2, 4))
                        fn_txt = corr_fn(file_type='xiwedges', **txt_kwargs)
                        rebinned.save_txt(fn_txt, wedges=(-1., -2./3, -1./3., 0., 1./3, 2./3, 1.))
                    elif corr_type == 'rppi':
                        fn_txt = corr_fn(file_type='xirppi', **txt_kwargs)
                        rebinned.save_txt(fn_txt)
                        fn_txt = corr_fn(file_type='wp', **txt_kwargs)
                        rebinned.save_txt(fn_txt, pimax=40.)
                    elif corr_type == 'theta':
                        fn_txt = corr_fn(file_type='theta', **txt_kwargs)
                        rebinned.save_txt(fn_txt)
                    
                    if args.vis and (mpicomm is None or mpicomm.rank == mpiroot):
                        if corr_type == 'smu':
                            sep, xis = rebinned(ells=(0, 2, 4), return_sep=True, return_std=False)
                        elif corr_type == 'rppi':
                            sep, xis = rebinned(pimax=40, return_sep=True, return_std=False)
                        else:
                            sep, xis = rebinned(return_sep=True, return_std=False)
                        if args.bin_type == 'log':
                            for xi in xis: plt.loglog(sep, xi)
                        if args.bin_type == 'lin':
                            for xi in xis: plt.plot(sep, sep**2 * xi)
                        tracers = tracer
                        if tracer2 is not None: tracers += ' ' + tracer2
                        plt.title('{} {:.2f} < z {:.2f} in {}'.format(tracers, zmin, zmax, region))
                        plt.show()
