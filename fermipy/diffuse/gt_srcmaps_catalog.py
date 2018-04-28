# Licensed under a 3-clause BSD style license - see LICENSE.rst
"""
Run gtsrcmaps for a single energy plane for a single source

This is useful to parallize the production of the source maps
"""
from __future__ import absolute_import, division, print_function

import os
import sys
import argparse
import math

import xml.etree.cElementTree as ElementTree

import BinnedAnalysis as BinnedAnalysis
import pyLikelihood as pyLike

from fermipy import utils
from fermipy.jobs.file_archive import FileFlags
from fermipy.jobs.link import add_argument, Link
from fermipy.jobs.scatter_gather import ConfigMaker, build_sg_from_link
from fermipy.jobs.slac_impl import make_nfs_path, get_slac_default_args, Slac_Interface

from fermipy.diffuse.name_policy import NameFactory
from fermipy.diffuse.binning import Component
from fermipy.diffuse.catalog_src_manager import make_catalog_comp_dict
from fermipy.diffuse.source_factory import make_sources
from fermipy.diffuse import defaults as diffuse_defaults


NAME_FACTORY = NameFactory()

class GtSrcmapsCatalog(Link):
    """Small class to create and write srcmaps for all the catalog sources, 
    once source at a time.

    This is useful for creating source maps for all the sources in a catalog
    """
    NULL_MODEL = 'srcmdls/null.xml'
 
    appname = 'fermipy-srcmaps-catalog'
    linkname_default = 'srcmaps-catalog'
    usage = '%s [options]' %(appname)
    description = "Run gtsrcmaps for for all the sources in a catalog"

    default_options = dict(irfs=diffuse_defaults.gtopts['irfs'],
                           expcube=diffuse_defaults.gtopts['expcube'],
                           bexpmap=diffuse_defaults.gtopts['bexpmap'],
                           cmap=diffuse_defaults.gtopts['cmap'],
                           srcmdl=diffuse_defaults.gtopts['srcmdl'],
                           outfile=diffuse_defaults.gtopts['outfile'],
                           srcmin=(0, 'Index of first source', int),
                           srcmax=(-1, 'Index of last source', int),
                           gzip=(False, 'Compress output file', bool))

    default_file_args = dict(expcube=FileFlags.input_mask,
                             cmap=FileFlags.input_mask,
                             bexpmap=FileFlags.input_mask,
                             srcmdl=FileFlags.input_mask,
                             outfile=FileFlags.output_mask)

    def __init__(self, **kwargs):
        """C'tor
        """
        linkname, init_dict = self._init_dict(**kwargs)
        super(GtSrcmapsCatalog, self).__init__(linkname, **init_dict)
 
    def run_analysis(self, argv):
        """Run this analysis"""
        args = self._parser.parse_args(argv)
        obs = BinnedAnalysis.BinnedObs(irfs=args.irfs,
                                       expCube=args.expcube,
                                       srcMaps=args.cmap,
                                       binnedExpMap=args.bexpmap)

        like = BinnedAnalysis.BinnedAnalysis(obs,
                                             optimizer='MINUIT',
                                             srcModel=GtSrcmapsCatalog.NULL_MODEL,
                                             wmap=None)

        source_factory = pyLike.SourceFactory(obs.observation)
        source_factory.readXml(args.srcmdl, BinnedAnalysis._funcFactory,
                               False, True, True)

        srcNames = pyLike.StringVector()
        source_factory.fetchSrcNames(srcNames)

        min_idx = args.srcmin
        max_idx = args.srcmax
        if max_idx < 0:
            max_idx = srcNames.size();

        for i in xrange(min_idx, max_idx):
            if i == min_idx:
                like.logLike.saveSourceMaps(args.outfile)
                pyLike.CountsMapBase.copyAndUpdateDssKeywords(args.cmap,
                                                              args.outfile,
                                                              None,
                                                              args.irfs)

            srcName = srcNames[i]
            source = source_factory.releaseSource(srcName)
            like.logLike.addSource(source, False)
            like.logLike.saveSourceMap_partial(args.outfile, source)
            like.logLike.deleteSource(srcName)

        if args.gzip:
            os.system("gzip -9 %s" % args.outfile)


class SrcmapsCatalog_SG(ConfigMaker):
    """Small class to generate configurations for gtsrcmaps for catalog sources

    This takes the following arguments:
    --comp     : binning component definition yaml file
    --data     : datset definition yaml file
    --library  : Yaml file with input source model definitions
    --make_xml : Write xml files for the individual components
    --nsrc     : Number of sources per job
    """
    appname = 'fermipy-srcmaps-catalog-sg'
    usage = "%s [options]" % (appname)
    description = "Run gtsrcmaps for catalog sources"
    clientclass = GtSrcmapsCatalog

    batch_args = get_slac_default_args()    
    batch_interface = Slac_Interface(**batch_args)

    default_options = dict(comp=diffuse_defaults.diffuse['comp'],
                           data=diffuse_defaults.diffuse['data'],
                           library=diffuse_defaults.diffuse['library'],
                           nsrc=(500, 'Number of sources per job', int),
                           make_xml=diffuse_defaults.diffuse['make_xml'])

    def __init__(self, link, **kwargs):
        """C'tor
        """
        super(SrcmapsCatalog_SG, self).__init__(link,
                                                options=kwargs.get('options',
                                                                   self.default_options.copy()))
        self.link = link

    @staticmethod
    def _make_xml_files(catalog_info_dict, comp_info_dict):
        """Make all the xml file for individual components
        """
        for val in catalog_info_dict.values():
            print("%s : %06i" % (val.srcmdl_name, len(val.roi_model.sources)))
            val.roi_model.write_xml(val.srcmdl_name)

        for val in comp_info_dict.values():
            for val2 in val.values():
                print("%s : %06i" % (val2.srcmdl_name, len(val2.roi_model.sources)))
                val2.roi_model.write_xml(val2.srcmdl_name)

    def build_job_configs(self, args):
        """Hook to build job configurations
        """
        job_configs = {}

        components = Component.build_from_yamlfile(args['comp'])
        NAME_FACTORY.update_base_dict(args['data'])

        ret_dict = make_catalog_comp_dict(sources=args['library'], 
                                          basedir=NAME_FACTORY.base_dict['basedir'])
        catalog_info_dict = ret_dict['catalog_info_dict']
        comp_info_dict = ret_dict['comp_info_dict']

        n_src_per_job = args['nsrc']

        if args['make_xml']:
            SrcmapsCatalog_SG._make_xml_files(catalog_info_dict, comp_info_dict)

        for catalog_name, catalog_info in catalog_info_dict.items():

            n_cat_src = len(catalog_info.catalog.table)
            n_job = int(math.ceil(float(n_cat_src)/n_src_per_job))

            for comp in components:
                zcut = "zmax%i" % comp.zmax
                key = comp.make_key('{ebin_name}_{evtype_name}')
                name_keys = dict(zcut=zcut,
                                 sourcekey=catalog_name,
                                 ebin=comp.ebin_name,
                                 psftype=comp.evtype_name,
                                 coordsys=comp.coordsys,
                                 irf_ver=NAME_FACTORY.irf_ver(),
                                 mktime='none',
                                 fullpath=True)

                for i_job in range(n_job):
                    full_key = "%s_%02i"%(key, i_job)
                    srcmin = i_job*n_src_per_job
                    srcmax = min(srcmin+n_src_per_job, n_cat_src)
                    outfile = NAME_FACTORY.srcmaps(**name_keys).replace('.fits', "_%02i.fits"%(i_job))
                    logfile = make_nfs_path(outfile.replace('.fits', '.log'))
                    job_configs[full_key] = dict(cmap=NAME_FACTORY.ccube(**name_keys),
                                                 expcube=NAME_FACTORY.ltcube(**name_keys),
                                                 irfs=NAME_FACTORY.irfs(**name_keys),
                                                 bexpmap=NAME_FACTORY.bexpcube(**name_keys),
                                                 outfile=outfile,
                                                 logfile=logfile,
                                                 srcmdl=catalog_info.srcmdl_name,
                                                 evtype=comp.evtype,
                                                 srcmin=srcmin,
                                                 srcmax=srcmax)

        return job_configs


def register_classes():
    GtSrcmapsCatalog.register_class()
    SrcmapsCatalog_SG.register_class()
