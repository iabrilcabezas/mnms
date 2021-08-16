from mnms import simio, tiled_ndmap, utils, soapack_utils as s_utils, tiled_noise, wav_noise, inpaint
from pixell import enmap, wcsutils
import healpy as hp
from enlib import bench
from optweight import wavtrans

import numpy as np

from abc import ABC, abstractmethod


class NoiseModel(ABC):

    def __init__(self, *qids, data_model=None, mask=None, ivar=None, imap=None, calibrated=True, downgrade=1,
                 mask_version=None, mask_name=None, union_sources=None, notes=None, **kwargs):
        """Base class for all NoiseModel subclasses. Supports loading raw data necessary for all 
        subclasses, such as masks and ivars. Also defines some class methods usable in subclasses.

        Parameters
        ----------
        qids : str
            One or more qids to incorporate in model.
        data_model : soapack.DataModel, optional
            DataModel instance to help load raw products, by default None.
            If None, will load the `default_data_model` from the `mnms` config.
        mask : enmap.ndmap, optional
            Data mask, by default None.
            If None, will load a mask according to the `mask_version` and `mask_name` kwargs.
        ivar : array-like, optional
            Data inverse-variance maps, by default None.
            If None, will be loaded via DataModel according to `downgrade` and `calibrated` kwargs.
        imap : array-like, optional
            Data maps, by default None.
            If None, will be loaded in call to NoiseModel.get_model(...), and may be retained in 
            memory if keep_data=True is passed to that function.
        calibrated : bool, optional
            Whether to load calibrated raw data, by default True.
        downgrade : int, optional
            The factor to downgrade map pixels by, by default 1.
        mask_version : str, optional
           The mask version folder name, by default None.
           If None, will first look in config `mnms` block, then block of default data model.
        mask_name : str, optional
            Name of mask file, by default None.
            If None, a default mask will be loaded from disk.
        union_sources : str, optional
            A soapack source catalog, by default None. If given, inpaint data and ivar maps.
        notes : str, optional
            A descriptor string to differentiate this instance from
            otherwise identical instances, by default None.
        kwargs : dict, optional
            Optional keyword arguments to pass to simio.get_sim_mask_fn (currently just
            `galcut` and `apod_deg`), by default None.
        """

        # if mask and ivar are provided, there is no way of checking whether calibrated, what downgrade, etc

        # store basic set of instance properties
        self._qids = qids
        if data_model is None:
            data_model = utils.get_default_data_model()
        self._data_model = data_model
        self._calibrated = calibrated
        self._downgrade = downgrade
        if mask_version is None:
            mask_version = utils.get_default_mask_version()
        self._mask_version = mask_version
        self._mask_name = mask_name
        self._notes = notes
        self._union_sources = union_sources
        self._kwargs = kwargs

        # get derived instance properties
        self._num_arrays = len(self._qids)
        self._num_splits = utils.get_nsplits_by_qid(self._qids[0], self._data_model)
        self._use_default_mask = mask_name is None

        # Get ivars and mask -- every noise model needs these for every operation.
        if mask is not None:
            self._mask = mask
        else:
            self._mask = self._get_mask()
        if ivar is not None:
            self._ivar = ivar
        else:
            self._ivar = self._get_ivar()

        # Possibly store input data
        self._imap = imap

        # sanity checks
        assert self._num_splits == self._ivar.shape[-4], \
            'Num_splits inferred from ivar shape != num_splits from data model table'

    def _get_mask(self):
        """Load the data mask from disk according to instance attributes.

        Returns
        -------
        mask : (ny, nx) enmap
            Sky mask. Dowgraded if requested.
        """

        for i, qid in enumerate(self._qids):
            with bench.show(f'Loading mask for {qid}'):
                fn = simio.get_sim_mask_fn(
                    qid, self._data_model, use_default_mask=self._use_default_mask,
                    mask_version=self._mask_version, mask_name=self._mask_name, **self._kwargs
                )
                mask = enmap.read_map(fn)

            # check that we are using the same mask for each qid -- this is required!
            if i == 0:
                main_mask = mask
            else:
                with bench.show(f'Checking mask compatibility between {qid} and {self._qids[0]}'):
                    assert np.allclose(
                        mask, main_mask), 'qids do not share a common mask -- this is required!'
                    assert wcsutils.is_compatible(
                        mask.wcs, main_mask.wcs), 'qids do not share a common mask wcs -- this is required!'
                    
        if self._downgrade != 1:
            with bench.show('Downgrading mask'):
                main_mask = main_mask.downgrade(self._downgrade)
        return main_mask

    def _get_ivar(self):
        """Load the inverse-variance maps according to instance attributes.

        Returns
        -------
        ivars : (nmaps, nsplits, npol, ny, nx) enmap
            Inverse-variance maps, possibly downgraded.
        """
        # first check for mask compatibility and get map geometry
        shape, wcs = self._check_geometry()

        # load the first ivar map geometry so that we may allocate a buffer to accumulate
        # all ivar maps in -- this has shape (nmaps, nsplits, npol, ny, nx).
        ivars = self._empty(ivar=True)

        # finally, fill the buffer
        for i, qid in enumerate(self._qids):
            with bench.show(self._action_str(qid, ivar=True)):
                if self._calibrated:
                    mul = s_utils.get_mult_fact(self._data_model, qid, ivar=True)
                else:
                    mul = 1

                # we want to do this split-by-split in case we can save
                # memory by downgrading one split at a time
                for j in range(self._num_splits):
                    ivar = s_utils.read_map(self._data_model, qid, j, ivar=True)
                    ivar = enmap.extract(ivar, shape, wcs)
                    
                    if self._downgrade != 1:
                        ivar = ivar.downgrade(self._downgrade, op=np.sum)
                    
                    ivars[i, j] = ivar * mul
        return ivars

    def _get_data(self):
        """Load data maps according to instance attributes, only performed during `get_model` call"""
        # first check for mask compatibility and get map geometry
        shape, wcs = self._check_geometry()

        # load the first data map geometry so that we may allocate a buffer to accumulate
        # all data maps in -- this has shape (nmaps, nsplits, npol, ny, nx)
        imaps = self._empty(ivar=False)

        # now fill in the buffer
        for i, qid in enumerate(self._qids):
            with bench.show(self._action_str(qid, ivar=False)):
                if self._calibrated:
                    mul = s_utils.get_mult_fact(self._data_model, qid, ivar=False)
                else:
                    mul = 1

                # possibly prepare required ivar_upgrade, mask_bool
                # for inpainting
                if self._union_sources:                
                    if self._downgrade != 1:
                        # Upgrade the mask and ivar, this is a good approximation.
                        ivar_up = enmap.upgrade(self._ivar[i], self._downgrade)
                        ivar_up /= self._downgrade ** 2
                        if i == 0:
                            mask_bool = utils.get_mask_bool(
                                enmap.upgrade(self._mask, self._downgrade))
                    else:
                        ivar_up = self._ivar[i]
                        if i == 0:
                            mask_bool = utils.get_mask_bool(self._mask)

                # we want to do this split-by-split in case we can save
                # memory by downgrading one split at a time
                for j in range(self._num_splits):
                    imap = s_utils.read_map(self._data_model, qid, j, ivar=False)
                    imap = enmap.extract(imap, shape, wcs)

                    if self._union_sources:
                        self._inpaint(imap, ivar_up[j], mask_bool, qid=qid, split_num=j) 

                    if self._downgrade != 1:
                        imap = imap.downgrade(self._downgrade)
                    
                    imaps[i, j] = imap * mul
        return imaps

    def _inpaint(self, imap, ivar, mask, inplace=True, qid=None, split_num=None):
        """Inpaint point sources given by the union catalog in input map.

        Parameters
        ---------
        imap : (..., 3, Ny, Nx) enmap
            Maps to be inpainted.
        ivar : (Ny, Nx) or (..., 1, Ny, Nx) enmap
            Inverse variance map. If not 2d, shape[:-3] must match imap.
        mask : (Ny, Nx) bool array
            Mask, True in observed regions.
        inplace : bool, optional
            Modify input map.
        qid : str, optional
            Array identifier, used to determine seed for inpainting.
        split_num : int, optional
            The 0-based index of the split that is inpainted, used to get unique seeds 
            per split if this function is called per split. Otherwise defaults to 0.
        """

        assert self._union_sources is not None, f'Inpainting needs union-sources, got {self._union_sources}'

        catalog = utils.get_catalog(self._union_sources)
        mask_bool = utils.get_mask_bool(mask)

        if qid:
            # This makes sure each qid gets a unique seed. The sim index is fixed.
            split_idx = 0 if split_num is None else split_num
            seed = utils.get_seed(*(split_idx, 999_999_999, self._data_model, qid))
        else:
            seed = None

        return inpaint.inpaint_noise_catalog(imap, ivar, mask_bool, catalog, inplace=inplace, 
                                             seed=seed)

    def _check_geometry(self):
        """Check that each qid in this instance's qids has compatible shape and wcs with its mask.
        """
        for i, qid in enumerate(self._qids):
            fn = simio.get_sim_mask_fn(
                qid, self._data_model, use_default_mask=self._use_default_mask,
                mask_version=self._mask_version, mask_name=self._mask_name, **self._kwargs
            )
            shape, wcs = enmap.read_map_geometry(fn)

            # check that we are using the same mask for each qid -- this is required!
            if i == 0:
                main_shape, main_wcs = shape, wcs
            else:
                with bench.show(f'Checking mask compatibility between {qid} and {self._qids[0]}'):
                    assert(
                        shape == main_shape), 'qids do not share a common mask wcs -- this is required!'
                    assert wcsutils.is_compatible(
                        wcs, main_wcs), 'qids do not share a common mask wcs -- this is required!'
        return main_shape, main_wcs

    def _empty(self, ivar=False):
        """Allocate an empty buffer that will broadcast against the Noise Model 
        number of arrays, number of splits, and the map (or ivar) shape.

        Parameters
        ----------
        ivar : bool, optional
            If True, load the inverse-variance map shape for the qid and
            split. If False, load the source-free map shape for the same,
            by default False.

        Returns
        -------
        enmap.ndmap
            An empty ndmap with shape (num_arrays, num_splits, num_pol, ny, nx),
            with dtype of the instance soapack.DataModel. If ivar is True, num_pol
            likely is 1. If ivar is False, num_pol likely is 3.
        """
        # read geometry from the map to be loaded. we really just need the first component,
        # a.k.a "npol"
        shape, _ = s_utils.read_map_geometry(self._data_model, self._qids[0], 0, ivar=ivar)
        shape = (shape[0], *self._mask.shape)
        shape = (self._num_arrays, self._num_splits, *shape)
        return enmap.empty(shape, wcs=self._mask.wcs, dtype=self._data_model.dtype)

    def _action_str(self, qid, ivar=False):
        """Get a string for benchmarking the loading step of a map product.

        Parameters
        ----------
        qid : str
            Map identification string
        ivar : bool, optional
            If True, print 'ivar' where appropriate. If False, print 'imap'
            where appropriate, by default False.

        Returns
        -------
        str
            Benchmarking action string.

        Examples
        --------
        >>> from mnms import noise_models as nm
        >>> tnm = nm.TiledNoiseModel('s18_03', downgrade=2, union_sources='20210209_sncut_10_aggressive', notes='my_model')
        >>> tnm._action_str('s18_03')
        >>> 'Loading, inpainting, downgrading imap for s18_03'
        """
        if ivar:
            if self._downgrade != 1:
                return f'Loading, downgrading ivar for {qid}'
            else:
                return f'Loading ivar for {qid}'
        else:
            if self._downgrade != 1 and self._union_sources:
                return f'Loading, inpainting, downgrading imap for {qid}'
            elif self._downgrade != 1 and not self._union_sources:
                return f'Loading, downgrading imap for {qid}'
            elif self._downgrade == 1 and self._union_sources:
                return f'Loading, inpainting imap for {qid}'
            else:
                return f'Loading imap for {qid}'
        
    @abstractmethod
    def _get_model_fn(self):
        pass

    @abstractmethod
    def _get_sim_fn(self):
        pass

    @abstractmethod
    def get_model(self):
        pass

    @abstractmethod
    def _get_model_from_disk(self):
        pass

    @abstractmethod
    def get_sim(self):
        pass

    @property
    def num_splits(self):
        return self._num_splits


class TiledNoiseModel(NoiseModel):

    def __init__(self, *qids, data_model=None, mask=None, ivar=None, imap=None, calibrated=True,
                 downgrade=1, mask_version=None, mask_name=None, union_sources=None, notes=None,
                 width_deg=4., height_deg=4., delta_ell_smooth=400, lmax=None, **kwargs):
        """A TiledNoiseModel object supports drawing simulations which capture spatially-varying
        noise correlation directions in map-domain data. They also capture the total noise power
        spectrum, spatially-varying map depth, and array-array correlations.

        Parameters
        ----------
        qids : str
            One or more qids to incorporate in model.
        data_model : soapack.DataModel, optional
            DataModel instance to help load raw products, by default None.
            If None, will load the `default_data_model` from the `mnms` config.
        mask : enmap.ndmap, optional
            Data mask, by default None.
            If None, will load a mask according to the `mask_version` and `mask_name` kwargs.
        ivar : array-like, optional
            Data inverse-variance maps, by default None.
            If None, will be loaded via DataModel according to `downgrade` and `calibrated` kwargs.
        imap : array-like, optional
            Data maps, by default None.
            If None, will be loaded in call to NoiseModel.get_model(...), and may be retained in 
            memory if keep_data=True is passed to that function.
        calibrated : bool, optional
            Whether to load calibrated raw data, by default True.
        downgrade : int, optional
            The factor to downgrade map pixels by, by default 1.
        mask_version : str, optional
           The mask version folder name, by default None.
           If None, will first look in config `mnms` block, then block of default data model.
        mask_name : str, optional
            Name of mask file, by default None.
            If None, a default mask will be loaded from disk.
        union_sources : str, optional
            A soapack source catalog, by default None.
        notes : str, optional
            A descriptor string to differentiate this instance from
            otherwise identical instances, by default None.
        width_deg : scalar, optional
            The characteristic tile width in degrees, by default 4.
        height_deg : scalar, optional
            The characteristic tile height in degrees,, by default 4.
        delta_ell_smooth : int, optional
            The smoothing scale in Fourier space to mitigate bias in the noise model
            from a small number of data splits, by default 400.
        lmax : int, optional
            The bandlimit of the maps, by default None.
            If None, will be set to twice the theoretical CAR limit, ie 180/wcs.wcs.cdelt[1].
        kwargs : dict, optional
            Optional keyword arguments to pass to simio.get_sim_mask_fn (currently just
            `galcut` and `apod_deg`), by default None.

        Notes
        -----
        Unless passed explicitly, the mask and ivar will be loaded at object instantiation time, 
        and stored as instance attributes.

        Raises
        ------
        NotImplementedError
            Inpainting is not implemented, so `union_sources` must be left None.

        Examples
        --------
        >>> from mnms import noise_models as nm
        >>> tnm = nm.TiledNoiseModel('s18_03', 's18_04', downgrade=2, notes='my_model')
        >>> tnm.get_model() # will take several minutes and require a lot of memory
                            # if running this exact model for the first time, otherwise
                            # will return None if model exists on-disk already
        >>> imap = tnm.get_sim(0, 123) # will get a sim of split 1 from the correlated arrays;
                                       # the map will have "index" 123, which is used in making
                                       # the random seed whether or not the sim is saved to disk,
                                       # and will be recorded in the filename if saved to disk.
        >>> print(imap.shape)
        >>> (2, 1, 3, 5600, 21600)
        """
        super().__init__(
            *qids, data_model=data_model, mask=mask, ivar=ivar, calibrated=calibrated, downgrade=downgrade,
            mask_version=mask_version, mask_name=mask_name,
            notes=notes, union_sources=union_sources, **kwargs
        )

        # save model-specific info
        self._width_deg = width_deg
        self._height_deg = height_deg
        self._delta_ell_smooth = delta_ell_smooth
        if lmax is None:
            lmax = utils.lmax_from_wcs(self._mask.wcs)
        self._lmax = lmax

        # initialize unloaded noise model
        self._covsqrt = None
        self._sqrt_cov_ell = None

    def _get_model_fn(self):
        return simio.get_tiled_model_fn(
            self._qids, self._width_deg, self._height_deg, self._delta_ell_smooth, self._lmax, notes=self._notes,
            data_model=self._data_model, mask_version=self._mask_version, bin_apod=self._use_default_mask, mask_name=self._mask_name,
            calibrated=self._calibrated, downgrade=self._downgrade, union_sources=self._union_sources, **self._kwargs
        )

    def _get_sim_fn(self, split_num, sim_num):
        # only difference w.r.t. above is split_num, sim_num
        return simio.get_tiled_sim_fn(
            self._qids, self._width_deg, self._height_deg, self._delta_ell_smooth, self._lmax, split_num, sim_num, notes=self._notes,
            data_model=self._data_model, mask_version=self._mask_version, bin_apod=self._use_default_mask, mask_name=self._mask_name,
            calibrated=self._calibrated, downgrade=self._downgrade, union_sources=self._union_sources, **self._kwargs
        )

    def get_model(self, check_on_disk=True, write=True, keep=False, keep_data=False, verbose=False,
                    nthread=0, flat_triu_axis=1):
        """Generate (or load) a sqrt-covariance matrix for this TiledNoiseModel instance.

        Parameters
        ----------
        check_on_disk : bool, optional
            If True, first check if an identical model (including by `notes`) exists
            on disk. If it does, do nothing or store it in the object attributes,
            depending on the `keep` kwarg. If it does not, generate the model
            instead. By default True.
        write : bool, optional
            Save the generated model to disk, by default True.
        keep : bool, optional
            Store the generated (or loaded) model in the instance attributes, by 
            default False.
        keep_data: bool, optional
            Store the loaded raw data splits in the instance attributes, by 
            default False.
        verbose : bool, optional
            Print possibly helpful messages, by default False.
        nthread : int, optional
            Number of threads to use, by default 0.
            If 0, use the maximum number of detectable cores (see `utils.get_cpu_count`)
        flat_triu_axis : int, optional
            The axis of the sqrt-covariance matrix buffer that will hold the upper-triangle
            of the matrix, by default 1.
        """
        if check_on_disk:
            try:
                self._get_model_from_disk(keep=keep)
                return
            except FileNotFoundError:
                print('Model not found on disk, generating instead')

        # the model needs data, so we need to load it
        imap = self._get_data()
        with bench.show('Generating noise model'):
            covsqrt, sqrt_cov_ell = tiled_noise.get_tiled_noise_covsqrt(
                imap, ivar=self._ivar, mask=self._mask, width_deg=self._width_deg, height_deg=self._height_deg, 
                delta_ell_smooth=self._delta_ell_smooth, lmax=self._lmax,
                nthread=nthread, verbose=verbose
            )

        if write:
            fn = self._get_model_fn()
            covsqrt = utils.to_flat_triu(
                covsqrt, axis1=flat_triu_axis, axis2=flat_triu_axis+1, flat_triu_axis=flat_triu_axis
            )
            tiled_ndmap.write_tiled_ndmap(
                fn, covsqrt, extra_header={'FLAT_TRIU_AXIS': flat_triu_axis}, extra_hdu={'SQRT_COV_ELL': sqrt_cov_ell}
            )

        if keep:
            self._covsqrt = covsqrt
            self._sqrt_cov_ell = sqrt_cov_ell

        if keep_data:
            self._imap = imap

    def _get_model_from_disk(self, keep=True):
        """Load a sqrt-covariance matrix from disk. If keep, store it in instance attributes."""
        fn = self._get_model_fn()
        covsqrt, extra_header, extra_hdu = tiled_ndmap.read_tiled_ndmap(
            fn, extra_header=['FLAT_TRIU_AXIS'], extra_hdu=['SQRT_COV_ELL']
        )
        flat_triu_axis = extra_header['FLAT_TRIU_AXIS']
        sqrt_cov_ell = extra_hdu['SQRT_COV_ELL']

        wcs = covsqrt.wcs
        tiled_info = covsqrt.tiled_info()
        covsqrt = utils.from_flat_triu(
            covsqrt, axis1=flat_triu_axis, axis2=flat_triu_axis+1, flat_triu_axis=flat_triu_axis
        )
        covsqrt = enmap.ndmap(covsqrt, wcs)
        covsqrt = tiled_ndmap.tiled_ndmap(covsqrt, **tiled_info)

        if keep:
            self._covsqrt = covsqrt
            self._sqrt_cov_ell = sqrt_cov_ell

    def get_sim(self, split_num, sim_num, nthread=0, check_on_disk=True, write=False, verbose=False):
        """Generate a tiled sim from this TiledNoiseModel.

        Parameters
        ----------
        split_num : int
            The 0-based index of the split to simulate.
        sim_num : int
            The map index, used in setting the random seed. Must be non-negative. If the sim
            is written to disk, this will be recorded in the filename. There is a maximum of
            9999, ie, one cannot have more than 10_000 of the same sim, of the same split, 
            from the same noise model (including the `notes`).
        nthread : int, optional
            Number of threads to use, by default 0.
            If 0, use the maximum number of detectable cores (see `utils.get_cpu_count`)
        check_on_disk : bool, optional
            If True, first check if the exact sim (including the noise model `notes`), 
            exists and disk, and if it does, load and return it. If it does not,
            generate the sim on-the-fly instead, by default True.
        write : bool, optional
            Save the generated sim to disk, by default False.
        verbose : bool, optional
            Print possibly helpful messages, by default False.

        Returns
        -------
        enmap.ndmap
            A sim of this noise model with the specified sim num, with shape
            (num_arrays, num_splits, num_pol, ny, nx), even if some of these
            axes have dimension 1. As implemented, num_splits is always 1. 
        """
        assert sim_num <= 9999, 'Cannot use a map index greater than 9999'

        fn = self._get_sim_fn(split_num, sim_num)

        if check_on_disk:
            try:
                return enmap.read_map(fn)
            except FileNotFoundError:
                print('Sim not found on disk, generating instead')

        if self._covsqrt is None:
            if verbose:
                print('Model not loaded, loading from disk')
            try:
                self._get_model_from_disk()
            except FileNotFoundError:
                fn = self._get_model_fn()
                print(f'Model does not exist on-disk, please generate it first')
                raise FileNotFoundError(fn)

        seed = utils.get_seed(
            *(split_num, sim_num, self._data_model, *self._qids))

        with bench.show('Generating noise sim'):
            smap = tiled_noise.get_tiled_noise_sim(
                self._covsqrt, ivar=self._ivar, sqrt_cov_ell=self._sqrt_cov_ell, 
                num_arrays=self._num_arrays, num_splits=self._num_splits,
                split_num=split_num, seed=seed, nthread=nthread, verbose=verbose
            )
            smap *= self._mask

        if write:
            enmap.write_map(fn, smap)
        return smap


class WaveletNoiseModel(NoiseModel):

    def __init__(self, *qids, data_model=None, mask=None, ivar=None, imap=None, calibrated=True,
                downgrade=1, mask_version=None, mask_name=None, union_sources=None, notes=None,
                lamb=1.3, lmax=None, smooth_loc=False, **kwargs):
        """A WaveletNoiseModel object supports drawing simulations which capture scale-dependent, 
        spatially-varying map depth. They also capture the total noise power spectrum, and 
        array-array correlations.

        Parameters
        ----------
        qids : str
            One or more qids to incorporate in model.
        data_model : soapack.DataModel, optional
            DataModel instance to help load raw products, by default None.
            If None, will load the `default_data_model` from the `mnms` config.
        mask : enmap.ndmap, optional
            Data mask, by default None.
            If None, will load a mask according to the `mask_version` and `mask_name` kwargs.
        ivar : array-like, optional
            Data inverse-variance maps, by default None.
            If None, will be loaded via DataModel according to `downgrade` and `calibrated` kwargs.
        imap : array-like, optional
            Data maps, by default None.
            If None, will be loaded in call to NoiseModel.get_model(...), and may be retained in 
            memory if keep_data=True is passed to that function.
        calibrated : bool, optional
            Whether to load calibrated raw data, by default True.
        downgrade : int, optional
            The factor to downgrade map pixels by, by default 1.
        mask_version : str, optional
           The mask version folder name, by default None.
           If None, will first look in config `mnms` block, then block of default data model.
        mask_name : str, optional
            Name of mask file, by default None.
            If None, a default mask will be loaded from disk.
        union_sources : str, optional
            A soapack source catalog, by default None.
        notes : str, optional
            A descriptor string to differentiate this instance from
            otherwise identical instances, by default None.
        lamb : float, optional
            Parameter specifying width of wavelets kernels in log(ell), by default 1.3
        lmax : int, optional
            The bandlimit of the maps, by default None.
            If None, will be set to exactly the theoretical CAR limit, ie 90/wcs.wcs.cdelt[1].
        smooth_loc : bool, optional
            If passed, use smoothing kernel that varies over the map, smaller along edge of 
            mask, by default False.
        kwargs : dict, optional
            Optional keyword arguments to pass to simio.get_sim_mask_fn (currently just
            `galcut` and `apod_deg`), by default None.

        Notes
        -----
        Unless passed explicitly, the mask and ivar will be loaded at object instantiation time, 
        and stored as instance attributes.

        Raises
        ------
        NotImplementedError
            Inpainting is not implemented, so `union_sources` must be left None.

        Examples
        --------
        >>> from mnms import noise_models as nm
        >>> wnm = nm.WaveletNoiseModel('s18_03', 's18_04', downgrade=2, notes='my_model')
        >>> wnm.get_model() # will take several minutes and require a lot of memory
                            # if running this exact model for the first time, otherwise
                            # will return None if model exists on-disk already
        >>> imap = wnm.get_sim(0, 123) # will get a sim of split 1 from the correlated arrays;
                                       # the map will have "index" 123, which is used in making
                                       # the random seed whether or not the sim is saved to disk,
                                       # and will be recorded in the filename if saved to disk.
        >>> print(imap.shape)
        >>> (2, 1, 3, 5600, 21600)
        """
        super().__init__(
            *qids, data_model=data_model, mask=mask, ivar=ivar, calibrated=calibrated, downgrade=downgrade, mask_version=mask_version,
            mask_name=mask_name, union_sources=union_sources, notes=notes, **kwargs
        )

        # save model-specific info
        self._lamb = lamb
        if lmax is None:
            lmax = wav_noise.lmax_from_wcs(self._mask.wcs)
        self._lmax = lmax
        self._smooth_loc = smooth_loc

        # save correction factors for later
        with bench.show('Getting correction factors'):
            corr_fact = utils.get_corr_fact(self._ivar)
            corr_fact = enmap.extract(
                corr_fact, self._mask.shape, self._mask.wcs)
            self._corr_fact = corr_fact

        # initialize unloaded noise model
        self._sqrt_cov_wavs = {}
        self._sqrt_cov_ells = {}
        self._w_ells = {}

    def _get_model_fn(self, split_num):
        return simio.get_wav_model_fn(
            self._qids, split_num, self._lamb, self._lmax, self._smooth_loc, notes=self._notes,
            data_model=self._data_model, mask_version=self._mask_version, bin_apod=self._use_default_mask, mask_name=self._mask_name,
            calibrated=self._calibrated, downgrade=self._downgrade, union_sources=self._union_sources, **self._kwargs
        )

    def _get_sim_fn(self, split_num, sim_num, alm=False):
        # only difference w.r.t. above is sim_num and alm flag
        return simio.get_wav_sim_fn(
            self._qids, split_num, self._lamb, self._lmax, self._smooth_loc, sim_num, alm=alm, notes=self._notes,
            data_model=self._data_model, mask_version=self._mask_version, bin_apod=self._use_default_mask, mask_name=self._mask_name,
            calibrated=self._calibrated, downgrade=self._downgrade, union_sources=self._union_sources, **self._kwargs
        )

    def get_model(self, check_on_disk=True, write=True, keep=False, keep_data=False, verbose=False):
        """Generate (or load) a sqrt-covariance matrix for this WaveletNoiseModel instance.

        Parameters
        ----------
        check_on_disk : bool, optional
            If True, first check if an identical model (including by `notes`) exists
            on disk. If it does, do nothing or store it in the object attributes,
            depending on the `keep` kwarg. If it does not, generate the model
            instead. By default True.
        write : bool, optional
            Save the generated model to disk, by default True.
        keep : bool, optional
            Store the generated (or loaded) model in the instance attributes, by 
            default False.
        keep_data: bool, optional
            Store the loaded raw data splits in the instance attributes, by 
            default False.
        verbose : bool, optional
            Print possibly helpful messages, by default False.
        """
        if check_on_disk:
            try:
                for s in range(self._num_splits):
                    self._get_model_from_disk(s, keep=keep)
                return
            except (FileNotFoundError, OSError):
                print('Model not found on disk, generating instead')

        # the model needs data, so we need to load it
        imap = self._get_data()
        imap = utils.get_noise_map(imap, self._ivar)

        sqrt_cov_wavs = {}
        sqrt_cov_ells = {}
        w_ells = {}
        with bench.show('Generating noise model'):
            for s in range(self._num_splits):
                sqrt_cov_wav, sqrt_cov_ell, w_ell = wav_noise.estimate_sqrt_cov_wav_from_enmap(
                    imap[:, s], self._mask, self._lmax, lamb=self._lamb, smooth_loc=self._smooth_loc
                )
                sqrt_cov_wavs[s] = sqrt_cov_wav
                sqrt_cov_ells[s] = sqrt_cov_ell
                w_ells[s] = w_ell

        if write:
            for s in range(self._num_splits):
                fn = self._get_model_fn(s)
                wavtrans.write_wav(
                    fn, sqrt_cov_wavs[s], symm_axes=[0, 1], extra={'sqrt_cov_ell': sqrt_cov_ells[s], 'w_ell': w_ells[s]}
                )

        if keep:
            self._sqrt_cov_wavs.update(sqrt_cov_wavs)
            self._sqrt_cov_ells.update(sqrt_cov_ells)
            self._w_ells.update(w_ells)

        if keep_data:
            self._imap = imap

    def _get_model_from_disk(self, split_num, keep=True):
        """Load a sqrt-covariance matrix from disk. If keep, store it in instance attributes."""
        sqrt_cov_wavs = {}
        sqrt_cov_ells = {}
        w_ells = {}

        fn = self._get_model_fn(split_num)
        sqrt_cov_wav, extra_dict = wavtrans.read_wav(
            fn, extra=['sqrt_cov_ell', 'w_ell']
        )
        sqrt_cov_ell = extra_dict['sqrt_cov_ell']
        w_ell = extra_dict['w_ell']
        sqrt_cov_wavs[split_num] = sqrt_cov_wav
        sqrt_cov_ells[split_num] = sqrt_cov_ell
        w_ells[split_num] = w_ell

        if keep:
            self._sqrt_cov_wavs.update(sqrt_cov_wavs)
            self._sqrt_cov_ells.update(sqrt_cov_ells)
            self._w_ells.update(w_ells)

    def get_sim(self, split_num, sim_num, alm=False, check_on_disk=True, write=False, verbose=False):
        """Generate a wavelet sim from this WaveletNoiseModel.

        Parameters
        ----------
        split_num : int
            The 0-based index of the split to simulate.
        sim_num : int
            The map index, used in setting the random seed. Must be non-negative. If the sim
            is written to disk, this will be recorded in the filename. There is a maximum of
            9999, ie, one cannot have more than 10_000 of the same sim, of the same split, 
            from the same noise model (including the `notes`).
        alm : bool, optional
            Generate simulated alms instead of a simulated map, by default False
        check_on_disk : bool, optional
            If True, first check if the exact sim (including the noise model `notes`), 
            exists and disk, and if it does, load and return it. If it does not,
            generate the sim on-the-fly instead, by default True.
        write : bool, optional
            Save the generated sim to disk, by default False.
        verbose : bool, optional
            Print possibly helpful messages, by default False.

        Returns
        -------
        enmap.ndmap
            A sim of this noise model with the specified sim num, with shape
            (num_arrays, num_splits, num_pol, ny, nx), even if some of these
            axes have dimension 1. As implemented, num_splits is always 1. 

        Raises
        ------
        NotImplementedError
            Generating alms instead of a map is not implemented, so `alm` must 
            be left False.
        """
        if alm:
            raise NotImplementedError(
                'Generating sims as alms not yet implemented')

        assert sim_num <= 9999, 'Cannot use a map index greater than 9999'

        fn = self._get_sim_fn(split_num, sim_num, alm=alm)

        if check_on_disk:
            try:
                if alm:
                    return hp.read_alm(fn)
                else:
                    return enmap.read_map(fn)
            except FileNotFoundError:
                print('Sim not found on disk, generating instead')

        if split_num not in self._sqrt_cov_wavs:
            if verbose:
                print(f'Model for split {split_num} not loaded, loading from disk')
            try:
                self._get_model_from_disk(split_num)
            except (FileNotFoundError, OSError):
                fn = self._get_model_fn(split_num)
                print(f'Model does not exist on-disk, please generate it first')
                raise FileNotFoundError(fn)

        seed = utils.get_seed(
            *(split_num, sim_num, self._data_model, *self._qids))

        with bench.show('Generating noise sim'):
            if alm:
                sim, _ = wav_noise.rand_alm_from_sqrt_cov_wav(
                    self._sqrt_cov_wavs[split_num], self._sqrt_cov_ells[split_num], self._lmax, self._w_ells[split_num],
                    dtype=np.complex64, seed=seed
                )
                assert sim.ndim == 3, 'Alm must have shape (num_arrays, num_pol, nelem)'
            else:
                sim = wav_noise.rand_enmap_from_sqrt_cov_wav(
                    self._sqrt_cov_wavs[split_num], self._sqrt_cov_ells[split_num], self._mask, self._lmax, self._w_ells[split_num],
                    dtype=np.float32, seed=seed
                )
                sim *= self._corr_fact[:, split_num]*self._mask
                assert sim.ndim == 4, 'Map must have shape (num_arrays, num_pol, ny, nx)'

        # want shape (num_arrays, num_splits=1, num_pol, ny, nx)
        sim = sim.reshape(sim.shape[0], 1, *sim.shape[1:])
        if write:
            if alm:
                hp.write_alm(fn, sim, overwrite=True)
            else:
                enmap.write_map(fn, sim)
        return sim
