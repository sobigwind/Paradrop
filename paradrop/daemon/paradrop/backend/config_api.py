"""
This module exposes device configuration.

Endpoints for these functions can be found under /api/v1/config.
"""

import json
from klein import Klein
from twisted.internet import reactor
from twisted.internet.defer import DeferredList, inlineCallbacks, returnValue

from paradrop.base import nexus, settings
from paradrop.base.output import out
from paradrop.base.pdutils import timeint, str2json
from paradrop.core.config import hostconfig
from paradrop.core.agent.http import PDServerRequest
from paradrop.core.agent.reporting import sendStateReport
from paradrop.core.agent.wamp_session import WampSession
from paradrop.confd import client as pdconf_client
from paradrop.lib.misc import ssh_keys

from . import cors


class ConfigApi(object):
    """
    Configuration API.

    This class handles HTTP API calls related to router configuration.
    """

    routes = Klein()

    def __init__(self, update_manager, update_fetcher):
        self.update_manager = update_manager
        self.update_fetcher = update_fetcher

    @routes.route('/hostconfig', methods=['PUT'])
    def update_hostconfig(self, request):
        """
        Replace the device's host configuration.

        **Example request**:

        .. sourcecode:: http

           PUT /api/v1/config/hostconfig
           Content-Type: application/json

           {
             "firewall": {
               "defaults": {
                 "forward": "ACCEPT",
                 "input": "ACCEPT",
                 "output": "ACCEPT"
               }
             },
             "lan": {
               "dhcp": {
                 "leasetime": "12h",
                 "limit": 100,
                 "start": 100
               },
               "firewall": {
                 "defaults": {
                   "conntrack": "1",
                   "forward": "ACCEPT",
                   "input": "ACCEPT",
                   "output": "ACCEPT"
                 },
                 "forwarding": [
                   {
                     "dest": "wan",
                     "src": "lan"
                   }
                 ]
               },
               "interfaces": [
                 "eth1",
                 "eth2"
               ],
               "ipaddr": "192.168.1.1",
               "netmask": "255.255.255.0",
               "proto": "static"
             },
             "system": {
               "autoUpdate": true,
               "chutePrefixSize": 24,
               "chuteSubnetPool": "192.168.128.0/17",
               "onMissingWiFi": "warn"
             },
             "telemetry": {
               "enabled": true,
               "interval": 60
             },
             "wan": {
               "firewall": {
                 "defaults": {
                   "conntrack": "1",
                   "forward": "ACCEPT",
                   "input": "ACCEPT",
                   "masq": "1",
                   "masq_src": [
                     "192.168.1.0/24",
                     "192.168.128.0/17"
                   ],
                   "output": "ACCEPT"
                 }
               },
               "interface": "eth0",
               "proto": "dhcp"
             },
             "wifi": [
               {
                 "channel": 1,
                 "htmode": "HT20",
                 "hwmode": "11g",
                 "id": "pci-wifi-0"
               },
               {
                 "channel": 36,
                 "htmode": "HT40+",
                 "hwmode": "11a",
                 "id": "pci-wifi-1",
                 "short_gi_40": true
               }
             ],
             "wifi-interfaces": [
               {
                 "device": "pci-wifi-0",
                 "maxassoc": 100,
                 "mode": "ap",
                 "network": "lan",
                 "ssid": "ParaDrop"
               }
             ],
             "zerotier": {
               "enabled": false,
               "networks": []
             }
           }

        **Example response**:

        .. sourcecode:: http

           HTTP/1.1 200 OK
           Content-Type: application/json

           {
             change_id: 1
           }

        The "wifi" section sets up physical device settings.
        Right now it is just the channel number.
        Other settings related to 11n or 11ac may go there as we implement them.

        The "wifi-interfaces" section sets up virtual interfaces.  Each virtual
        interface has an underlying physical device, but there can be multiple
        interfaces per device up to a limit set somewhere in the driver,
        firmware, or hardware.  Virtual interfaces can be configured as APs as
        in the example. They could also be set to client mode and connect to
        other APs, but this is not supported currently.

        Therefore, it enables one card in the sense that it starts an AP using
        one of the cards but does not start anything on the second card.  On the
        other hand, it enables two cards in the sense that it configures one
        card to use channel 1 and the second one to use channel 6, and a chute
        may start an AP on the second card.

        Here are a few ways we can modify the example configuration:
        - If we want to run a second AP on the second device, we can add a
          section to "wifi-interfaces" with device="wlan1" and ifname="wlan1".
        - If we want to run a second AP on the first device, we can add a
          section to "wifi-interfaces" with device="wlan0" and an ifname that is
          different from all others interfaces sharing the device.
          We should avoid anything that starts with "wlan" except the case
          where the name exactly matches the device.
          For device "wlan0", acceptable names would be "wlan0", "pd-wlan", etc.
          Avoid "vwlan0.X" and the like because that would conflict with chute interfaces,
          but "hwlan0.X" would be fine.
        - If we want to add WPA2, set encryption="psk2" and key="the passphrase"
          in the wifi-interface section for the AP.
          Based on standard, the Passphrase of WPA2 must be between 8 and 63 characters, inclusive.

        Advanced wifi device settings:
        - For 2.4 GHz channels, set hwmode="11g", and for 5 GHz, set hwmode="11a".
        It may default to 802.11b (bad, slow) otherwise.
        - For a 40 MHz channel width in 802.11n, set htmode="HT40+" or htmode="HT40-".
        Plus means add the next higher channel, and minus means add the lower channel.
        For example, setting channel=36 and htmode="HT40+" results in using
        channels 36 and 40.
        - If the hardware supports it, you can enable short guard interval
        for slightly higher data rates.  There are separate settings for each
        channel width, short_gi_20, short_gi_40, short_gi_80, short_gi_160.
        Most 11n hardware can support short_gi_40 at the very least.
        """
        cors.config_cors(request)
        body = str2json(request.content.read())
        config = body['config']

        update = dict(updateClass='ROUTER',
                      updateType='sethostconfig',
                      name=settings.RESERVED_CHUTE,
                      tok=timeint(),
                      hostconfig=config)

        # We will return the change ID to the caller for tracking and log
        # retrieval.
        update['change_id'] = self.update_manager.assign_change_id()

        d = self.update_manager.add_update(**update)

        result = {
            'change_id': update['change_id']
        }
        request.setHeader('Content-Type', 'application/json')
        return json.dumps(result)

    @routes.route('/hostconfig', methods=['GET'])
    def get_hostconfig(self, request):
        """
        Get the device's current host configuration.

        **Example request**:

        .. sourcecode:: http

           GET /api/v1/config/hostconfig

        **Example response**:

        .. sourcecode:: http

           HTTP/1.1 200 OK
           Content-Type: application/json

           {
             "firewall": {
               "defaults": {
                 "forward": "ACCEPT",
                 "input": "ACCEPT",
                 "output": "ACCEPT"
               }
             },
             ...
           }
        """
        cors.config_cors(request)
        config = hostconfig.prepareHostConfig()
        request.setHeader('Content-Type', 'application/json')
        return json.dumps(config, separators=(',',':'))


    @routes.route('/pdid', methods=['GET'])
    def get_pdid(self, request):
        """
        Get the device's current ParaDrop ID. This is the identifier assigned
        by the cloud controller.

        **Example request**:

        .. sourcecode:: http

           GET /api/v1/config/pdid

        **Example response**:

        .. sourcecode:: http

           HTTP/1.1 200 OK
           Content-Type: application/json

           {
             pdid: "5890e1e5ab7e317e6c6e049f"
           }
        """
        cors.config_cors(request)
        pdid = nexus.core.info.pdid
        if pdid is None:
            pdid = ""
        request.setHeader('Content-Type', 'application/json')
        return json.dumps({'pdid': pdid})


    @routes.route('/provision', methods=['POST'])
    def provision(self, request):
        """
        Provision the device with credentials from a cloud controller.
        """
        cors.config_cors(request)
        body = str2json(request.content.read())
        routerId = body['routerId']
        apitoken = body['apitoken']
        pdserver = body['pdserver']
        wampRouter = body['wampRouter']

        changed = False
        if routerId != nexus.core.info.pdid \
            or pdserver != nexus.core.info.pdserver \
            or wampRouter != nexus.core.info.wampRouter:
            if pdserver and wampRouter:
                nexus.core.provision(routerId, pdserver, wampRouter)
            else:
                nexus.core.provision(routerId)
            changed = True

        if apitoken != nexus.core.getKey('apitoken'):
            nexus.core.saveKey(apitoken, 'apitoken')
            changed = True

        if changed:
            PDServerRequest.resetToken()
            nexus.core.jwt_valid = False

            def set_update_fetcher(session):
                session.set_update_fetcher(self.update_fetcher)

            @inlineCallbacks
            def start_polling(result):
                yield self.update_fetcher.start_polling()

            def send_response(result):
                response = dict()
                response['provisioned'] = True
                response['httpConnected'] = nexus.core.jwt_valid
                response['wampConnected'] = nexus.core.wamp_connected
                request.setHeader('Content-Type', 'application/json')
                return json.dumps(response)

            wampDeferred = nexus.core.connect(WampSession)
            wampDeferred.addCallback(set_update_fetcher)

            httpDeferred = sendStateReport()
            httpDeferred.addCallback(start_polling)

            dl = DeferredList([wampDeferred, httpDeferred], consumeErrors=True)
            dl.addBoth(send_response)
            reactor.callLater(6, dl.cancel)
            return dl
        else:
            return json.dumps({'success': False,
                               'message': 'No change on the provision parameters'})


    @routes.route('/provision', methods=['GET'])
    def get_provision(self, request):
        """
        Get the provision status of the device.
        """
        cors.config_cors(request)
        result = dict()
        result['routerId'] = nexus.core.info.pdid
        result['pdserver'] = nexus.core.info.pdserver
        result['wampRouter'] = nexus.core.info.wampRouter
        apitoken = nexus.core.getKey('apitoken')
        result['provisioned'] = (result['routerId'] is not None and \
                                 apitoken is not None)
        result['httpConnected'] = nexus.core.jwt_valid
        result['wampConnected'] = nexus.core.wamp_connected
        request.setHeader('Content-Type', 'application/json')
        return json.dumps(result)


    @routes.route('/startUpdate', methods=['POST'])
    def start_update(self, request):
        cors.config_cors(request)
        updateManager.startUpdate()
        request.setHeader('Content-Type', 'application/json')
        return json.dumps({'success': True})


    @routes.route('/factoryReset', methods=['POST'])
    @inlineCallbacks
    def factory_reset(self, request):
        """
        Initiate the factory reset process.
        """
        cors.config_cors(request)
        update = dict(updateClass='ROUTER',
                      updateType='factoryreset',
                      name='PARADROP',
                      tok=timeint())
        update = yield self.update_manager.add_update(**update)
        returnValue(json.dumps(update.result))

    @routes.route('/pdconf', methods=['GET'])
    def pdconf(self, request):
        """
        Get configuration sections from pdconf.

        This returns a list of configuration sections and whether they were
        successfully applied. This is intended for debugging purposes.
        """
        cors.config_cors(request)
        request.setHeader('Content-Type', 'application/json')
        return pdconf_client.systemStatus()

    @routes.route('/pdconf', methods=['PUT'])
    def pdconf_reload(self, request):
        """
        Trigger pdconf to reload UCI configuration files.

        Trigger pdconf to reload UCI configuration files and return the status.
        This function is intended for low-level debugging of the paradrop
        pdconf module.
        """
        cors.config_cors(request)
        request.setHeader('Content-Type', 'application/json')
        return pdconf_client.reloadAll()

    @routes.route('/sshKeys/<user>', methods=['GET', 'POST'])
    def sshKeys(self, request, user):
        """
        Manage list of authorized keys for SSH access.
        """
        cors.config_cors(request)
        request.setHeader('Content-Type', 'application/json')

        if request.method == "GET":
            try:
                keys = ssh_keys.getAuthorizedKeys(user)
                return json.dumps(keys)
            except Exception as e:
                out.warn(str(e))
                request.setResponseCode(404)
                return json.dumps({'message': str(e)})
        else:
            body = str2json(request.content.read())
            key = body['key'].strip()

            try:
                ssh_keys.addAuthorizedKey(key, user)
                return json.dumps(body)
            except Exception as e:
                out.warn(str(e))
                request.setResponseCode(404)
                return json.dumps({'message': str(e)})


# The following lines are for compatibility with the autoflask documentation
# generator.
ConfigApi.routes.static_path = ""
ConfigApi.routes.view_functions = ConfigApi.routes._endpoints
