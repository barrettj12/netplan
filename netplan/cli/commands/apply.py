#!/usr/bin/python3
#
# Copyright (C) 2018 Canonical, Ltd.
# Author: Mathieu Trudel-Lapierre <mathieu.trudel-lapierre@canonical.com>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; version 3.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

'''netplan apply command line'''

import logging
import os
import sys
import glob
import subprocess

import netplan.cli.utils as utils
from netplan.configmanager import ConfigManager, ConfigurationError

import netifaces

log = logging.getLogger("netplan.apply")


class NetplanApply(utils.NetplanCommand):

    def __init__(self):
        super().__init__(command_id='apply',
                         description='Apply current netplan config to running system',
                         leaf=True)

    def run(self):  # pragma: nocover (covered in autopkgtest)
        self.func = NetplanApply.command_apply

        self.parse_args()
        self.run_command()

    @staticmethod
    def command_apply(run_generate=True, sync=False, exit_on_error=True):  # pragma: nocover (covered in autopkgtest)
        old_files_networkd = bool(glob.glob('/run/systemd/network/*netplan-*'))
        old_files_nm = bool(glob.glob('/run/NetworkManager/system-connections/netplan-*'))

        if run_generate and subprocess.call([utils.get_generator_path()]) != 0:
            if exit_on_error:
                sys.exit(os.EX_CONFIG)
            else:
                raise ConfigurationError("the configuration could not be generated")

        config_manager = ConfigManager()
        devices = netifaces.interfaces()

        # Re-start service when
        # 1. We have configuration files for it
        # 2. Previously we had config files for it but not anymore
        # Ideally we should compare the content of the *netplan-* files before and
        # after generation to minimize the number of re-starts, but the conditions
        # above works too.
        restart_networkd = bool(glob.glob('/run/systemd/network/*netplan-*'))
        if not restart_networkd and old_files_networkd:
            restart_networkd = True
        restart_nm = bool(glob.glob('/run/NetworkManager/system-connections/netplan-*'))
        if not restart_nm and old_files_nm:
            restart_nm = True

        # stop backends
        if restart_networkd:
            log.debug('networkd configuration changed, stopping networkd')
            utils.systemctl_networkd('stop', sync=sync, extra_services=['netplan-wpa@*.service'])
        else:
            log.debug('no networkd configuration generated: not restarting networkd')

        if restart_nm:
            log.debug('NetworkManager configuration changed, stopping NetworkManager')
            if utils.nm_running():
                # restarting NM does not cause new config to be applied, need to shut down devices first
                for device in devices:
                    # ignore failures here -- some/many devices might not be managed by NM
                    try:
                        utils.nmcli(['device', 'disconnect', device])
                    except subprocess.CalledProcessError:
                        pass

                utils.systemctl_network_manager('stop', sync=sync)
        else:
            log.debug('no NetworkManager configuration generated: not restarting NetworkManager')

        # evaluate config for extra steps we need to take (like renaming)
        # for now, only applies to non-virtual (real) devices.
        config_manager.parse()
        changes = NetplanApply.process_link_changes(devices, config_manager)

        # if the interface is up, we can still apply some .link file changes
        for device in devices:
            log.debug('triggering .link rules for %s', device)
            subprocess.check_call(['udevadm', 'test-builtin',
                                   'net_setup_link',
                                   '/sys/class/net/' + device],
                                  stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)

        # apply renames to "down" devices
        for iface, settings in changes.items():
            if settings.get('name'):
                new_name = settings.get('name')
                log.debug('applying name change for %s: now called %s', iface, new_name)
                subprocess.check_call(['ip', 'link', 'set',
                                       'dev', iface,
                                       'name', new_name],
                                      stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL)

        subprocess.check_call(['udevadm', 'settle'])

        # (re)start backends
        if restart_networkd:
            log.debug('re-starting networkd')
            netplan_wpa = [os.path.basename(f) for f in glob.glob('/run/systemd/system/*.wants/netplan-wpa@*.service')]
            log.debug('starting extra networkd services: %s', ",".join(netplan_wpa))
            utils.systemctl_networkd('start', sync=sync, extra_services=netplan_wpa)
        if restart_nm:
            log.debug('re-starting NetworkManager')
            utils.systemctl_network_manager('start', sync=sync)

    @staticmethod
    def is_composite_member(composites, phy):  # pragma: nocover (covered in autopkgtest)
        """
        Is this physical interface a member of a 'composite' virtual
        interface? (bond, bridge)
        """
        for composite in composites:
            for id, settings in composite.items():
                members = settings.get('interfaces', [])
                for iface in members:
                    if iface == phy:
                        log.debug("%s is a member of %s", phy, id)
                        return True

        return False

    @staticmethod
    def process_link_changes(interfaces, config_manager):  # pragma: nocover (covered in autopkgtest)
        """
        Go through the pending changes and pick what needs special
        handling. Only applies to "down" interfaces which can be safely
        updated.
        """

        changes = {}
        phys = dict(config_manager.physical_interfaces)
        composite_interfaces = [config_manager.bridges, config_manager.bonds]

        # TODO (cyphermox): factor out some of this matching code (and make it
        # pretty) in its own module.
        matches = {'by-driver': {},
                   'by-mac': {},
                   }
        for phy, settings in phys.items():
            if not settings:
                continue
            if phy == 'renderer':
                continue
            newname = settings.get('set-name')
            if not newname:
                continue
            match = settings.get('match')
            if not match:
                continue
            driver = match.get('driver')
            mac = match.get('macaddress')
            if driver:
                matches['by-driver'][driver] = newname
            if mac:
                matches['by-mac'][mac] = newname

        # /sys/class/net/ens3/device -> ../../../virtio0
        # /sys/class/net/ens3/device/driver -> ../../../../bus/virtio/drivers/virtio_net
        for interface in interfaces:
            if interface not in phys:
                # do not rename  virtual devices
                log.debug('Skipping non-physical interface: %s', interface)
                continue
            if NetplanApply.is_composite_member(composite_interfaces, interface):
                log.debug('Skipping composite member %s', interface)
                # do not rename members of virtual devices. MAC addresses
                # may be the same for all interface members.
                continue
            # try to get the device's driver for matching.
            devdir = os.path.join('/sys/class/net', interface)
            try:
                with open(os.path.join(devdir, 'operstate')) as f:
                    state = f.read().strip()
                    if state != 'down':
                        log.debug('device %s operstate is %s, not changing', interface, state)
                        continue
            except IOError as e:
                log.error('Cannot determine operstate of %s: %s', interface, str(e))
                continue

            try:
                driver = os.path.realpath(os.path.join(devdir, 'device', 'driver'))
                driver_name = os.path.basename(driver)
            except IOError as e:
                log.debug('Cannot replug %s: cannot read link %s/device: %s', interface, devdir, str(e))
                driver_name = None
                pass

            link = netifaces.ifaddresses(interface)[netifaces.AF_LINK][0]
            macaddress = link.get('addr')
            if driver_name in matches['by-driver']:
                new_name = matches['by-driver'][driver_name]
                log.debug(new_name)
                log.debug(interface)
                if new_name != interface:
                    changes.update({interface: {'name': new_name}})
            if macaddress in matches['by-mac']:
                new_name = matches['by-mac'][macaddress]
                if new_name != interface:
                    changes.update({interface: {'name': new_name}})

        log.debug('Link changes: %s', changes)
        return changes
