import asyncio
import email
import imaplib
import json
import logging
import re
import requests
import traceback
from typing import List

import scrypted_sdk
from scrypted_sdk import ScryptedDeviceBase
from scrypted_sdk.types import Setting, SettingValue, Settings, DeviceProvider, ScryptedInterface

from .arlo import Arlo
from .arlo.arlo_async import change_stream_class
from .arlo.logging import logger as arlo_lib_logger
from .logging import ScryptedDeviceLoggerMixin
from .util import BackgroundTaskMixin
from .camera import ArloCamera
from .doorbell import ArloDoorbell
from .basestation import ArloBasestation
from .base import ArloDeviceBase


class ArloProvider(ScryptedDeviceBase, Settings, DeviceProvider, ScryptedDeviceLoggerMixin, BackgroundTaskMixin):
    arlo_cameras = None
    arlo_basestations = None
    _arlo_mfa_code = None
    scrypted_devices = None
    _arlo = None
    _arlo_mfa_complete_auth = None

    plugin_verbosity_choices = {
        "Normal": logging.INFO,
        "Verbose": logging.DEBUG
    }

    arlo_transport_choices = ["MQTT", "SSE"]

    mfa_strategy_choices = ["Manual", "IMAP"]

    def __init__(self, nativeId: str = None) -> None:
        super().__init__(nativeId=nativeId)
        self.logger_name = "provider"

        self.arlo_cameras = {}
        self.arlo_basestations = {}
        self.scrypted_devices = {}
        self.imap = None
        self.imap_signal = None
        self.imap_skip_emails = None

        self.propagate_verbosity()
        self.propagate_transport()

        def load(self):
            if self.mfa_strategy == "IMAP":
                self.initialize_imap()
            else:
                _ = self.arlo

        asyncio.get_event_loop().call_soon(load, self)
        self.create_task(self.onDeviceEvent(ScryptedInterface.Settings.value, None))

    def print(self, *args, **kwargs) -> None:
        """Overrides the print() from ScryptedDeviceBase to avoid double-printing in the main plugin console."""
        print(*args, **kwargs)

    @property
    def arlo_username(self) -> str:
        return self.storage.getItem("arlo_username")

    @property
    def arlo_password(self) -> str:
        return self.storage.getItem("arlo_password")

    @property
    def arlo_auth_headers(self) -> str:
        return self.storage.getItem("arlo_auth_headers")

    @property
    def arlo_user_id(self) -> str:
        return self.storage.getItem("arlo_user_id")

    @property
    def arlo_transport(self) -> str:
        transport = self.storage.getItem("arlo_transport")
        if transport is None or transport not in ArloProvider.arlo_transport_choices:
            transport = "SSE"
            self.storage.setItem("arlo_transport", transport)
        return transport

    @property
    def plugin_verbosity(self) -> str:
        verbosity = self.storage.getItem("plugin_verbosity")
        if verbosity is None or verbosity not in ArloProvider.plugin_verbosity_choices:
            verbosity = "Normal"
            self.storage.setItem("plugin_verbosity", verbosity)
        return verbosity

    @property
    def mfa_strategy(self) -> str:
        strategy = self.storage.getItem("mfa_strategy")
        if strategy is None or strategy not in ArloProvider.mfa_strategy_choices:
            strategy = "Manual"
            self.storage.setItem("mfa_strategy", strategy)
        return strategy

    @property
    def refresh_interval(self) -> int:
        interval = self.storage.getItem("refresh_interval")
        if interval is None:
            interval = 90
            self.storage.setItem("refresh_interval", interval)
        return int(interval)

    @property
    def imap_mfa_host(self) -> str:
        return self.storage.getItem("imap_mfa_host")

    @property
    def imap_mfa_port(self) -> int:
        port = self.storage.getItem("imap_mfa_port")
        if port is None:
            port = 993
            self.storage.setItem("imap_mfa_port", port)
        return int(port)

    @property
    def imap_mfa_username(self) -> str:
        return self.storage.getItem("imap_mfa_username")

    @property
    def imap_mfa_password(self) -> str:
        return self.storage.getItem("imap_mfa_password")

    @property
    def imap_mfa_interval(self) -> int:
        interval = self.storage.getItem("imap_mfa_interval")
        if interval is None:
            interval = 7
            self.storage.setItem("imap_mfa_interval", interval)
        return int(interval)

    @property
    def arlo(self) -> Arlo:
        if self._arlo is not None:
            if self._arlo_mfa_complete_auth is not None:
                if self._arlo_mfa_code == "":
                    return None

                self.logger.info("Completing Arlo MFA...")
                self._arlo_mfa_complete_auth(self._arlo_mfa_code)
                self._arlo_mfa_complete_auth = None
                self._arlo_mfa_code = None
                self.logger.info("Arlo MFA done")

                self.storage.setItem("arlo_auth_headers", json.dumps(dict(self._arlo.request.session.headers.items())))
                self.storage.setItem("arlo_user_id", self._arlo.user_id)

                self.create_task(self.do_arlo_setup())

            return self._arlo

        if not self.arlo_username or not self.arlo_password:
            return None

        self.logger.info("Trying to initialize Arlo client...")
        try:
            self._arlo = Arlo(self.arlo_username, self.arlo_password)
            headers = self.arlo_auth_headers
            if headers:
                self._arlo.UseExistingAuth(self.arlo_user_id, json.loads(headers))
                self.logger.info(f"Initialized Arlo client, reusing stored auth headers")

                self.create_task(self.do_arlo_setup())
                return self._arlo
            else:
                self._arlo_mfa_complete_auth = self._arlo.LoginMFA()
                self.logger.info(f"Initialized Arlo client, waiting for MFA code")
                return None
        except Exception as e:
            traceback.print_exc()
            self._arlo = None
            self._arlo_mfa_code = None
            return None

    async def do_arlo_setup(self) -> None:
        try:
            await self.discover_devices()
            await self.arlo.Subscribe([
                (self.arlo_basestations[camera["parentId"]], camera) for camera in self.arlo_cameras.values()
            ])

            for nativeId in self.arlo_cameras.keys():
                await self.getDevice(nativeId)

            self.arlo.event_stream.set_refresh_interval(self.refresh_interval)
        except requests.exceptions.HTTPError as e:
            traceback.print_exc()
            self.logger.error(f"Error logging in, will retry with fresh login")
            self._arlo = None
            self._arlo_mfa_code = None
            self.storage.setItem("arlo_auth_headers", None)
            _ = self.arlo
        except Exception as e:
            traceback.print_exc()

    def invalidate_arlo_client(self) -> None:
        if self._arlo is not None:
            self._arlo.Unsubscribe()
        self._arlo = None
        self._arlo_mfa_code = None
        self._arlo_mfa_complete_auth = None
        self.storage.setItem("arlo_auth_headers", "")
        self.storage.setItem("arlo_user_id", "")

    def get_current_log_level(self) -> int:
        return ArloProvider.plugin_verbosity_choices[self.plugin_verbosity]

    def propagate_verbosity(self) -> None:
        self.print(f"Setting plugin verbosity to {self.plugin_verbosity}")
        log_level = self.get_current_log_level()
        self.logger.setLevel(log_level)
        for _, device in self.scrypted_devices.items():
            device.logger.setLevel(log_level)
        arlo_lib_logger.setLevel(log_level)

    def propagate_transport(self) -> None:
        self.print(f"Setting plugin transport to {self.arlo_transport}")
        change_stream_class(self.arlo_transport)

    def initialize_imap(self) -> None:
        if not self.imap_mfa_host or not self.imap_mfa_port or \
            not self.imap_mfa_username or not self.imap_mfa_password or \
            not self.imap_mfa_interval:
            return

        self.exit_imap()
        try:
            self.logger.info("Trying connect to IMAP")
            self.imap = imaplib.IMAP4_SSL(self.imap_mfa_host, port=self.imap_mfa_port)

            res, _ = self.imap.login(self.imap_mfa_username, self.imap_mfa_password)
            if res.lower() != "ok":
                raise Exception(f"IMAP login failed: {res}")
            res, _ = self.imap.select(mailbox="INBOX", readonly=True)
            if res.lower() != "ok":
                raise Exception(f"IMAP failed to fetch INBOX: {res}")

            # fetch existing arlo emails so we skip them going forward
            res, self.imap_skip_emails = self.imap.search(None, "FROM", "do_not_reply@arlo.com")
            if res.lower() != "ok":
                raise Exception(f"IMAP failed to fetch old Arlo emails: {res}")
        except Exception as e:
            traceback.print_exc()
            self.exit_imap()
        else:
            self.logger.info("Connected to IMAP")
            self.imap_signal = asyncio.Queue()
            self.create_task(self.imap_relogin_loop())

    def exit_imap(self) -> None:
        if self.imap_signal:
            self.imap_signal.put_nowait(None)
        self.imap_signal = None
        self.imap_skip_emails = None
        self.imap = None

    async def imap_relogin_loop(self) -> None:
        imap_signal = self.imap_signal
        self.logger.info(f"Starting IMAP refresh loop {id(imap_signal)}")
        while True:
            self.logger.info("Performing IMAP login flow")

            # save old client and details in case of error
            old_arlo = self._arlo
            old_headers = self.storage.getItem("arlo_auth_headers")
            old_user_id = self.storage.getItem("arlo_user_id")

            # clear everything
            self._arlo = None
            self._arlo_mfa_code = None
            self._arlo_mfa_complete_auth = None
            self.storage.setItem("arlo_auth_headers", "")
            self.storage.setItem("arlo_user_id", "")

            # initialize login and prompt for MFA
            _ = self.arlo

            # do imap lookup
            # adapted from https://github.com/twrecked/pyaarlo/blob/77c202b6f789c7104a024f855a12a3df4fc8df38/pyaarlo/tfa.py
            try:
                while True:
                    self.logger.info("Checking IMAP for MFA codes")

                    self.imap.check()
                    res, emails = self.imap.search(None, "FROM", "do_not_reply@arlo.com")
                    if res.lower() != "ok":
                        raise Exception("IMAP error: {res}")

                    if emails == self.imap_skip_emails:
                        self.logger.info("No new emails found, will sleep and retry")
                        await asyncio.sleep(1)
                        continue

                    skip_emails = self.imap_skip_emails[0].split()
                    def search_email(msg_id):
                        if msg_id in skip_emails:
                            return None

                        res, msg = self.imap.fetch(msg_id, "(BODY.PEEK[])")
                        if res.lower() != "ok":
                            raise Exception("IMAP error: {res}")

                        if isinstance(msg[0][1], bytes):
                            for part in email.message_from_bytes(msg[0][1]).walk():
                                if part.get_content_type() != "text/html":
                                    continue
                                try:
                                    for line in part.get_payload(decode=True).splitlines():
                                        code = re.match(r"^\W+(\d{6})\W*$", line.decode())
                                        if code is not None:
                                            return code.group(1)
                                except:
                                    continue
                        return None

                    for msg_id in emails[0].split():
                        res = search_email(msg_id)
                        if res is not None:
                            self._arlo_mfa_code = res
                            break

                    # update previously seen emails list
                    self.imap_skip_emails = emails

                    if self._arlo_mfa_code is not None:
                        self.logger.info("Found MFA code")
                        break

                    self.logger.info("No MFA code found, will sleep and retry")
                    await asyncio.sleep(1)
            except Exception as e:
                traceback.print_exc()
                self.logger.error("Will retry on next IMAP interval")
                self._arlo = old_arlo
                self.storage.setItem("arlo_auth_headers", old_headers)
                self.storage.setItem("arlo_user_id", old_user_id)
                self._arlo_mfa_code = None
                self._arlo_mfa_complete_auth = None
            else:
                # finish login
                if old_arlo:
                    old_arlo.Unsubscribe()
                _ = self.arlo

            # continue by sleeping/waiting for a signal
            interval = self.imap_mfa_interval * 24 * 60 * 60  # convert interval days to seconds
            signal_task = asyncio.create_task(imap_signal.get())

            # wait until either we receive a signal or the refresh interval expires
            done, pending = await asyncio.wait([signal_task, asyncio.sleep(interval)], return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()

            done_task = done.pop()
            if done_task is signal_task and done_task.result() is None:
                # exit signal received
                self.logger.info(f"Exiting IMAP refresh loop {id(imap_signal)}")
                return

    async def getSettings(self) -> List[Setting]:
        results = [
            {
                "group": "General",
                "key": "arlo_username",
                "title": "Arlo Username",
                "value": self.arlo_username,
            },
            {
                "group": "General",
                "key": "arlo_password",
                "title": "Arlo Password",
                "type": "password",
                "value": self.arlo_password,
            },
            {
                "group": "General",
                "key": "mfa_strategy",
                "title": "Two Factor Strategy",
                "description": "Mechanism to fetch the two factor code for Arlo login. Save after changing this field for more settings.",
                "value": self.mfa_strategy,
                "choices": self.mfa_strategy_choices,
            },
        ]

        if self.mfa_strategy == "Manual":
            results.extend([
                {
                    "group": "General",
                    "key": "arlo_mfa_code",
                    "title": "Two Factor Code",
                    "description": "Enter the code sent by Arlo to your email or phone number.",
                },
                {
                    "group": "General",
                    "key": "force_reauth",
                    "title": "Force Re-Authentication",
                    "description": "Resets the authentication flow of the plugin. Will also re-do 2FA.",
                    "value": False,
                    "type": "boolean",
                },
            ])
        else:
            results.extend([
                {
                    "group": "IMAP 2FA",
                    "key": "imap_mfa_host",
                    "title": "IMAP Hostname",
                    "value": self.imap_mfa_host,
                },
                {
                    "group": "IMAP 2FA",
                    "key": "imap_mfa_port",
                    "title": "IMAP Port",
                    "value": self.imap_mfa_port,
                },
                {
                    "group": "IMAP 2FA",
                    "key": "imap_mfa_username",
                    "title": "IMAP Username",
                    "value": self.imap_mfa_username,
                },
                {
                    "group": "IMAP 2FA",
                    "key": "imap_mfa_password",
                    "title": "IMAP Password",
                    "type": "password",
                    "value": self.imap_mfa_password,
                },
                {
                    "group": "IMAP 2FA",
                    "key": "imap_mfa_interval",
                    "title": "Refresh Login Interval",
                    "description": "Interval, in days, to refresh the login session to Arlo Cloud. "
                                   "Must be a value greater than 0.",
                    "type": "number",
                    "value": self.imap_mfa_interval,
                }
            ])

        results.extend([
            {
                "group": "General",
                "key": "arlo_transport",
                "title": "Underlying Transport Protocol",
                "description": "Select the underlying transport protocol used to connect to Arlo Cloud.",
                "value": self.arlo_transport,
                "choices": self.arlo_transport_choices,
            },
            {
                "group": "General",
                "key": "refresh_interval",
                "title": "Refresh Event Stream Interval",
                "description": "Interval, in minutes, to refresh the underlying event stream connection to Arlo Cloud. "
                               "A value of 0 disables this feature.",
                "type": "number",
                "value": self.refresh_interval,
            },
            {
                "group": "General",
                "key": "plugin_verbosity",
                "title": "Verbose Logging",
                "description": "Enable this option to show debug messages, including events received from connected Arlo cameras.",
                "value": self.plugin_verbosity == "Verbose",
                "type": "boolean",
            },
        ])

        return results

    async def putSetting(self, key: str, value: SettingValue) -> None:
        if not self.validate_setting(key, value):
            await self.onDeviceEvent(ScryptedInterface.Settings.value, None)
            return

        skip_arlo_client = False
        if key == "arlo_mfa_code":
            self._arlo_mfa_code = value
        elif key == "force_reauth":
            # force arlo client to be invalidated and reloaded
            self.invalidate_arlo_client()
        elif key == "plugin_verbosity":
            self.storage.setItem(key, "Verbose" if value == "true" else "Normal")
            self.propagate_verbosity()
            skip_arlo_client = True
        else:
            self.storage.setItem(key, value)

            if key == "arlo_transport":
                self.propagate_transport()
                # force arlo client to be invalidated and reloaded, but
                # keep any mfa codes
                if self._arlo is not None:
                    self._arlo.Unsubscribe()
                    self._arlo = None
            elif key == "mfa_strategy":
                if value == "IMAP":
                    self.initialize_imap()
                else:
                    self.exit_imap()
                skip_arlo_client = True
            elif key == "refresh_interval":
                if self._arlo is not None and self._arlo.event_stream:
                    self._arlo.event_stream.set_refresh_interval(self.refresh_interval)
                skip_arlo_client = True
            elif key.startswith("imap_mfa"):
                self.initialize_imap()
                skip_arlo_client = True
            else:
                # force arlo client to be invalidated and reloaded
                self.invalidate_arlo_client()

        if not skip_arlo_client:
            # initialize Arlo client or continue MFA
            _ = self.arlo
        await self.onDeviceEvent(ScryptedInterface.Settings.value, None)

    def validate_setting(self, key: str, val: SettingValue) -> bool:
        if key == "refresh_interval":
            try:
                val = int(val)
            except ValueError:
                self.logger.error(f"Invalid refresh interval '{val}' - must be an integer")
                return False
            if val < 0:
                self.logger.error(f"Invalid refresh interval '{val}' - must be nonnegative")
                return False
        elif key == "imap_mfa_port":
            try:
                val = int(val)
            except ValueError:
                self.logger.error(f"Invalid IMAP port '{val}' - must be an integer")
                return False
            if val < 0:
                self.logger.error(f"Invalid IMAP port '{val}' - must be nonnegative")
                return False
        elif key == "imap_mfa_interval":
            try:
                val = int(val)
            except ValueError:
                self.logger.error(f"Invalid IMAP interval '{val}' - must be an integer")
                return False
            if val < 1:
                self.logger.error(f"Invalid IMAP interval '{val}' - must be positive")
                return False
        return True

    @ArloDeviceBase.async_print_exception_guard
    async def discover_devices(self, duration: int = 0) -> None:
        if not self.arlo:
            raise Exception("Arlo client not connected, cannot discover devices")

        self.logger.info("Discovering devices...")
        self.arlo_cameras = {}
        self.arlo_basestations = {}
        self.scrypted_devices = {}

        camera_devices = []
        provider_to_device_map = {}

        basestations = self.arlo.GetDevices(['basestation', 'siren'])
        for basestation in basestations:
            nativeId = basestation["deviceId"]
            self.logger.debug(f"Adding {nativeId}")

            if nativeId in self.arlo_basestations:
                self.logger.info(f"Skipping basestation {nativeId} ({basestation['modelId']}) as it has already been added")
                continue
            self.arlo_basestations[nativeId] = basestation

            device = await self.getDevice(nativeId)
            scrypted_interfaces = device.get_applicable_interfaces()
            manifest = device.get_device_manifest()
            self.logger.debug(f"Interfaces for {nativeId} ({basestation['modelId']}): {scrypted_interfaces}")

            # for basestations, we want to add them to the top level DeviceProvider
            provider_to_device_map.setdefault(None, []).append(manifest)

            # we want to trickle discover them so they are added without deleting all existing
            # root level devices - this is for backward compatibility
            await scrypted_sdk.deviceManager.onDeviceDiscovered(manifest)

            # add any builtin child devices and trickle discover them
            child_manifests = device.get_builtin_child_device_manifests()
            for child_manifest in child_manifests:
                await scrypted_sdk.deviceManager.onDeviceDiscovered(child_manifest)
                provider_to_device_map.setdefault(child_manifest["providerNativeId"], []).append(child_manifest)

        self.logger.info(f"Discovered {len(basestations)} basestations")

        cameras = self.arlo.GetDevices(['camera', "arloq", "arloqs", "doorbell"])
        for camera in cameras:
            nativeId = camera["deviceId"]
            self.logger.debug(f"Adding {nativeId}")

            if camera["deviceId"] != camera["parentId"] and camera["parentId"] not in self.arlo_basestations:
                self.logger.info(f"Skipping camera {camera['deviceId']} ({camera['modelId']}) because its basestation was not found")
                continue

            if nativeId in self.arlo_cameras:
                self.logger.info(f"Skipping camera {nativeId} ({camera['modelId']}) as it has already been added")
                continue
            self.arlo_cameras[nativeId] = camera

            if camera["deviceId"] == camera["parentId"]:
                # these are standalone cameras with no basestation, so they act as their
                # own basestation
                self.arlo_basestations[camera["deviceId"]] = camera

            device: ArloDeviceBase = await self.getDevice(nativeId)
            scrypted_interfaces = device.get_applicable_interfaces()
            manifest = device.get_device_manifest()
            self.logger.debug(f"Interfaces for {nativeId} ({camera['modelId']}): {scrypted_interfaces}")

            if camera["deviceId"] == camera["parentId"]:
                provider_to_device_map.setdefault(None, []).append(manifest)
            else:
                provider_to_device_map.setdefault(camera["parentId"], []).append(manifest)

            # trickle discover this camera so it exists for later steps
            await scrypted_sdk.deviceManager.onDeviceDiscovered(manifest)

            # add any builtin child devices and trickle discover them
            child_manifests = device.get_builtin_child_device_manifests()
            for child_manifest in child_manifests:
                await scrypted_sdk.deviceManager.onDeviceDiscovered(child_manifest)
                provider_to_device_map.setdefault(child_manifest["providerNativeId"], []).append(child_manifest)

            camera_devices.append(manifest)

        if len(cameras) != len(camera_devices):
            self.logger.info(f"Discovered {len(cameras)} cameras, but only {len(camera_devices)} are usable")
        else:
            self.logger.info(f"Discovered {len(cameras)} cameras")

        for provider_id in provider_to_device_map.keys():
            if provider_id is None:
                continue
            await scrypted_sdk.deviceManager.onDevicesChanged({
                "devices": provider_to_device_map[provider_id],
                "providerNativeId": provider_id,
            })

        # ensure devices at the root match all that was discovered
        await scrypted_sdk.deviceManager.onDevicesChanged({
            "devices": provider_to_device_map[None]
        })

    async def getDevice(self, nativeId: str) -> ArloDeviceBase:
        ret = self.scrypted_devices.get(nativeId, None)
        if ret is None:
            ret = self.create_device(nativeId)
            if ret is not None:
                self.scrypted_devices[nativeId] = ret
        return ret

    def create_device(self, nativeId: str) -> ArloDeviceBase:
        if nativeId not in self.arlo_cameras and nativeId not in self.arlo_basestations:
            self.logger.warning(f"Cannot create device for nativeId {nativeId}, maybe it hasn't been loaded yet?")
            return None

        arlo_device = self.arlo_cameras.get(nativeId)
        if not arlo_device:
            # this is a basestation, so build the basestation object
            arlo_device = self.arlo_basestations[nativeId]
            return ArloBasestation(nativeId, arlo_device, self)

        if arlo_device["parentId"] not in self.arlo_basestations:
            self.logger.warning(f"Cannot create camera with nativeId {nativeId} when {arlo_device['parentId']} is not a valid basestation")
            return None
        arlo_basestation = self.arlo_basestations[arlo_device["parentId"]]

        if arlo_device["deviceType"] == "doorbell":
            return ArloDoorbell(nativeId, arlo_device, arlo_basestation, self)
        else:
            return ArloCamera(nativeId, arlo_device, arlo_basestation, self)