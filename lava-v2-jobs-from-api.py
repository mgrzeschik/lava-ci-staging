#!/usr/bin/python
#
# Copyright (C) 2016, 2017 Linaro Limited
# Author: Matt Hart <matthew.hart@linaro.org>
#
# Copyright (C) 2017 Collabora Ltd
# Author: Guillaume Tucker <guillaume.tucker@collabora.com>
#
# This module is free software; you can redistribute it and/or modify it under
# the terms of the GNU Lesser General Public License as published by the Free
# Software Foundation; either version 2.1 of the License, or (at your option)
# any later version.
#
# This library is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU Lesser General Public License for more
# details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this library; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA

import urllib2
import urlparse
import httplib
import re
import os
import shutil
import argparse
import ConfigParser
import json
import sys
import time
from lib import configuration, device_map
from lib.utils import setup_job_dir, write_file
import requests
import urlparse
import urllib
from jinja2 import Environment, FileSystemLoader


LEGACY_X86_PLATFORMS = ['x86', 'x86-kvm', 'x86-32']
ARCHS = ['arm64', 'arm64be', 'armeb', 'armel', 'x86']
ROOTFS_URL = 'http://storage.kernelci.org/images/rootfs'
INITRD_URL = '/'.join([ROOTFS_URL, 'buildroot/{}/rootfs.cpio.gz'])
NFSROOTFS_URL = '/'.join([ROOTFS_URL, 'buildroot/{}/rootfs.tar.xz'])
KSELFTEST_INITRD_URL = '/'.join([ROOTFS_URL, 'buildroot/{}/tests/rootfs.cpio.gz'])

def get_builds(api, token, config):
    headers = {
        "Authorization": token,
    }
    url_params = {
        'job': config.get('tree'),
        'kernel': config.get('describe'),
        'git_branch': config.get('branch'),
        'arch': config.get('arch'),
    }
    job_defconfig = config.get('defconfig_full')
    if job_defconfig:
        url_params['defconfig_full'] = job_defconfig
        n_configs = 1
    else:
        n_configs = int(config.get('defconfigs'))
    url_params = urllib.urlencode(url_params)
    url = urlparse.urljoin(api, 'build?{}'.format(url_params))

    print("Calling KernelCI API: {}".format(url))

    builds = []
    loops = 10
    retry_time = 30
    for loop in range(loops):
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        data = json.loads(response.content)
        builds = data['result']
        if len(builds) >= n_configs:
            break
        print("Got fewer builds ({}) than expected ({}), retry in {} seconds"
              .format(len(builds), n_configs, retry_time))
        time.sleep(retry_time)

    return builds


def get_job_params(config, template, opts, device, build, defconfig, dtb, plan):
    short_template_file = os.path.join(plan, template)
    template_file = os.path.join('templates', short_template_file)
    if not (template_file.endswith('.jinja2')
            and os.path.exists(template_file)):
        return None

    arch = config.get('arch')
    storage = config.get('storage')
    job_name = '-'.join([
        config.get('tree'), config.get('branch'), config.get('describe'),
        arch, defconfig[:100], dtb, device['device_type'], plan])

    url_px = '/'.join([
        build['job'], build['git_branch'], build['kernel'], arch, defconfig])
    base_url = urlparse.urljoin(storage, '/'.join([url_px, '']))
    kernel_url = urlparse.urljoin(
        storage, '/'.join([url_px, build['kernel_image']]))
    dtb_full = opts['dtb_full']
    if dtb_full.endswith('.dtb'):
        dtb_url = urlparse.urljoin(
            storage, '/'.join([url_px, 'dtbs', dtb_full]))
        platform = dtb[:-4]
    else:
        dtb_url = None
        platform = device['device_type']

    endian = 'big' if 'BIG_ENDIAN' in defconfig else 'little'
    initrd_arch = arch
    if arch == 'arm64':
        if endian == 'big':
            initrd_arch = 'arm64be'
    elif arch == 'arm':
        if endian == 'big':
            initrd_arch = 'armeb'
        else:
            initrd_arch = 'armel'

    nfsrootfs_url = None
    initrd_url = None
    if 'kselftest' in plan:
        initrd_url = KSELFTEST_INITRD_URL.format(initrd_arch)
    else:
        initrd_url = INITRD_URL.format(initrd_arch)
    if 'nfs' in plan:
        nfsrootfs_url = NFSROOTFS_URL.format(initrd_arch)
        initrd_url = None
    if build['modules']:
        modules_url = urlparse.urljoin(
            storage, '/'.join([url_px, build['modules']]))
    else:
        modules_url = None

    device_type = device['device_type']
    if device_type.startswith('qemu') or device_type == 'kvm':
        device_type = 'qemu'

    callback_name = 'lava/boot' if plan == 'boot' else 'lava/test'
    defconfig_base = ''.join(defconfig.split('+')[:1])

    job_params = {
        'name': job_name,
        'dtb_url': dtb_url,
        'dtb_short': dtb,
        'dtb_full': dtb_full,
        'platform': platform,
        'mach': device['mach'],
        'kernel_url': kernel_url,
        'image_type': 'kernel-ci',
        'image_url': base_url,
        'modules_url': modules_url,
        'plan': plan,
        'kernel': config.get('describe'),
        'tree': config.get('tree'),
        'defconfig': defconfig,
        'fastboot': str(device['fastboot']).lower(),
        'priority': config.get('priority'),
        'device_type': device['device_type'],
        'template_file': template_file,
        'base_url': base_url,
        'endian': endian,
        'short_template_file': short_template_file,
        'arch': arch,
        'git_branch': config.get('branch'),
        'git_commit': build['git_commit'],
        'git_describe': config.get('describe'),
        'git_url': build['git_url'],
        'defconfig_base': defconfig_base,
        'initrd_url': initrd_url,
        'kernel_image': build['kernel_image'],
        'nfsrootfs_url': nfsrootfs_url,
        'callback': config.get('callback'),
        'api': config.get('api'),
        'lab_name': config.get('lab'),
        'callback_name': callback_name,
        'context': device.get('context'),
    }

    job_params.update({k: opts[k] for k in [
        'arch_defconfig',
        'test_suite',
        'test_set',
        'test_desc',
        'test_type',
    ] if k in opts
    })

    return job_params


def job_is_valid(config, device, opts, defconfig, arch, dtb, plan):
    git_describe = config.get('describe')
    lab = config.get('lab')
    device_type = device['device_type']
    if defconfig in device['defconfig_blacklist']:
        print("defconfig {} is blacklisted for device {}"
              .format(defconfig, device_type))
    elif (device.has_key('defconfig_whitelist')
          and defconfig not in device['defconfig_whitelist']):
        print("defconfig {} is not in whitelist for device {}"
              .format(defconfig, device_type))
    elif device.has_key('arch_blacklist') and arch in device['arch_blacklist']:
        print("arch {} is blacklisted for device {}".format(arch, device_type))
    elif (device.has_key('lab_blacklist') and lab in device['lab_blacklist']):
        print("device {} is blacklisted for lab {}".format(device_type, lab))
    elif "BIG_ENDIAN" in defconfig and not device.get('boot_be', False):
        print("BIG_ENDIAN is not supported on {}".format(device_type))
    elif "LPAE" in defconfig and not device['lpae']:
        print("LPAE is not supported on {}".format(device_type))
    elif any([x for x in device['kernel_blacklist'] if x in git_describe]):
        print("git_describe {} is blacklisted for device {}"
              .format(git_describe, device_type))
    elif (any([x for x in device['nfs_blacklist'] if x in git_describe])
          and plan in ['boot-nfs', 'boot-nfs-mp']):
        print("git_describe {} is blacklisted for NFS on device {}"
              .format(git_describe, device_type))
    elif ('be_blacklist' in device
          and any([x for x in device['be_blacklist'] if x in git_describe])
          and device.get('boot_be', False)):
        print("git_describe {} is blacklisted for BE on device {}"
              .format(git_describe, device_type))
    elif (plan != 'boot'
          and opts['arch_defconfig'] not in opts['plan_defconfigs']):
        print("defconfig {} not in test plan {}"
              .format(opts['arch_defconfig'], plan))
    elif (config.get('targets') and device_type not in config.get('targets')):
        pass
    elif (arch == 'x86' and dtb == 'x86-32'
          and 'i386' not in opts['arch_defconfig']):
        print("{} is not a 32-bit x86 build, skipping for 32-bit device {}"
              .format(defconfig, device_type))
    elif 'kselftest' in defconfig and plan != 'kselftest':
        print("Skipping kselftest defconfig because plan was not kselftest")
    else:
        return True
    return False


def main(args):
    config = configuration.get_config(args)
    token = config.get('token')
    api = config.get('api')
    storage = config.get('storage')

    if not token:
        raise Exception("No KernelCI API token provided")
    if not api:
        raise Exception("No KernelCI API URL provided")
    if not storage:
        raise Exception("No KernelCI storage URL provided")

    print("Working on kernel {}/{}".format(
        config.get('tree'), config.get('branch')))

    builds = get_builds(api, token, config)
    print("Number of builds: {}".format(len(builds)))

    arch = config.get('arch')
    cwd = os.getcwd()
    jobs = []
    for build in builds:
        defconfig = build['defconfig_full']
        print("Working on build {}".format(' '.join(
            [config.get('tree'), config.get('branch'), config.get('describe'),
             arch, defconfig])))

        if build.get('status') != 'PASS':
            continue
        kimage = build.get('kernel_image')
        if not kimage:
            print("No kernel_image for {}".format(defconfig))
            continue
        if kimage == 'bzImage' and arch == 'x86':
            build['dtb_dir_data'].extend(LEGACY_X86_PLATFORMS)
        if arch in ['arm', 'arm64', 'x86'] and 'defconfig' in defconfig:
            build['dtb_dir_data'].append('qemu')

        for plan in config.get('plans'):
            opts = {
                'arch_defconfig': '-'.join([arch, defconfig]),
            }

            if plan != 'boot':
                pconfig = ConfigParser.ConfigParser()

                try:
                    pconfig.read(os.path.join(
                        cwd, 'templates', plan, '.'.join([plan, 'ini'])))
                except Exception, e:
                    print("Unable to load test configuration")
                    print(e)
                    continue

                plan_defconfigs = pconfig.get(plan, 'defconfigs').split(',')

                opts.update({
                    'test_suite': pconfig.get(plan, 'suite'),
                    'test_set': pconfig.get(plan, 'set'),
                    'test_desc': pconfig.get(plan, 'description'),
                    'test_type': pconfig.get(plan, 'type'),
                    'plan_defconfigs': plan_defconfigs,
                })

            for dtb in build['dtb_dir_data']:
                # hack for arm64 dtbs in subfolders
                opts['dtb_full'] = dtb
                if arch == 'arm64':
                    dtb = str(dtb).split('/')[-1]
                if dtb in device_map:
                    for device in device_map[dtb]:
                        if job_is_valid(config, device, opts, defconfig, arch, dtb, plan):
                            for template in device['templates']:
                                job_params = get_job_params(
                                    config, template, opts, device, build, defconfig, dtb, plan)
                                if job_params:
                                    jobs.append(job_params)

    job_dir = setup_job_dir(config.get('jobs') or config.get('lab'))
    for job in jobs:
        job_file = os.path.join(job_dir, '.'.join([job['name'], 'yaml']))
        with open(job_file, 'w') as f:
            f.write(jinja_render(job))
        print "Job written: %s" % job_file


def jinja_render(job):
    env = Environment(loader=FileSystemLoader('templates'))
    template = env.get_template(job['short_template_file'])
    return template.render(job)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", help="path to KernelCI configuration file")
    parser.add_argument("--token", help="KernelCI API Token")
    parser.add_argument("--api", help="KernelCI API URL")
    parser.add_argument("--storage", help="KernelCI storage URL")
    parser.add_argument("--lab", help="KernelCI Lab Name", required=True)
    parser.add_argument("--jobs", help="absolute path to top jobs folder")
    parser.add_argument("--tree", help="KernelCI build kernel tree", required=True)
    parser.add_argument("--branch", help="KernelCI build kernel branch", required=True)
    parser.add_argument("--describe", help="KernelCI build kernel git describe", required=True)
    parser.add_argument("--section", default="default", help="section in the KernelCI config file")
    parser.add_argument("--plans", nargs='+', required=True, help="test plan to create jobs for")
    parser.add_argument("--arch", help="specific architecture to create jobs for", required=True)
    parser.add_argument("--targets", nargs='+', help="specific targets to create jobs for")
    parser.add_argument("--priority", choices=['high', 'medium', 'low', 'HIGH', 'MEDIUM', 'LOW'],
                        help="priority for LAVA jobs", default='high')
    parser.add_argument("--callback", help="Add a callback notification to the Job YAML")
    parser.add_argument("--defconfigs", help="Expected number of defconfigs from the API", default=0)
    parser.add_argument("--defconfig_full",
                        help="Only look for builds from this full defconfig")
    args = vars(parser.parse_args())
    if args:
        main(args)
    else:
        exit(1)
