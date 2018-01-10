from UM.i18n import i18nCatalog
from UM.Logger import Logger
from UM.Settings.DefinitionContainer import DefinitionContainer
from UM.Application import Application

from UM.Settings.ContainerRegistry import ContainerRegistry
from cura.MachineAction import MachineAction

from PyQt5.QtCore import pyqtSignal, pyqtProperty, pyqtSlot, QUrl, QObject
from PyQt5.QtQml import QQmlComponent, QQmlContext
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtNetwork import QNetworkRequest, QNetworkAccessManager

import os.path
import json
import base64

catalog = i18nCatalog("cura")

class DiscoverRepetierAction(MachineAction):
    def __init__(self, parent = None):
        super().__init__("DiscoverRepetierAction", catalog.i18nc("@action", "Connect Repetier"))

        self._qml_url = "DiscoverRepetierAction.qml"
        self._window = None
        self._context = None

        self._network_plugin = None

        #   QNetwork manager needs to be created in advance. If we don't it can happen that it doesn't correctly
        #   hook itself into the event loop, which results in events never being fired / done.
        self._manager = QNetworkAccessManager()
        self._manager.finished.connect(self._onRequestFinished)

        self._settings_reply = None

        # Try to get version information from plugin.json
        plugin_file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plugin.json")
        try:
            with open(plugin_file_path) as plugin_file:
                plugin_info = json.load(plugin_file)
                plugin_version = plugin_info["version"]
        except:
            # The actual version info is not critical to have so we can continue
            plugin_version = "Unknown"
            Logger.logException("w", "Could not get version information for the plugin")

        self._user_agent = ("%s/%s %s/%s" % (
            Application.getInstance().getApplicationName(),
            Application.getInstance().getVersion(),
            "RepetierPlugin",
            Application.getInstance().getVersion()
        )).encode()


        self._instance_responded = False
        self._instance_api_key_accepted = False
        self._instance_supports_sd = False
        self._instance_supports_camera = False

        ContainerRegistry.getInstance().containerAdded.connect(self._onContainerAdded)
        Application.getInstance().engineCreatedSignal.connect(self._createAdditionalComponentsView)

    @pyqtSlot()
    def startDiscovery(self):
        if not self._network_plugin:
            self._network_plugin = Application.getInstance().getOutputDeviceManager().getOutputDevicePlugin("RepetierPlugin")
            self._network_plugin.addInstanceSignal.connect(self._onInstanceDiscovery)
            self._network_plugin.removeInstanceSignal.connect(self._onInstanceDiscovery)
            self._network_plugin.instanceListChanged.connect(self._onInstanceDiscovery)
            self.instancesChanged.emit()
        else:
            # Restart bonjour discovery
            self._network_plugin.startDiscovery()

    def _onInstanceDiscovery(self, *args):
        self.instancesChanged.emit()

    @pyqtSlot(str)
    def removeManualInstance(self, name):
        if not self._network_plugin:
            return

        self._network_plugin.removeManualInstance(name)

    @pyqtSlot(str, str, int, str, bool, str, str)
    def setManualInstance(self, name, address, port, path, useHttps, userName, password):
        # This manual printer could replace a current manual printer
        self._network_plugin.removeManualInstance(name)
        
        self._network_plugin.addManualInstance(name, address, port, path, useHttps, userName, password)

    def _onContainerAdded(self, container):
        # Add this action as a supported action to all machine definitions
        if isinstance(container, DefinitionContainer) and container.getMetaDataEntry("type") == "machine" and container.getMetaDataEntry("supports_usb_connection"):
            Application.getInstance().getMachineActionManager().addSupportedAction(container.getId(), self.getKey())

    instancesChanged = pyqtSignal()

    @pyqtProperty("QVariantList", notify = instancesChanged)
    def discoveredInstances(self):
        if self._network_plugin:
            instances = list(self._network_plugin.getInstances().values())
            instances.sort(key = lambda k: k.name)
            return instances
        else:
            return []

    @pyqtSlot(str)
    def setKey(self, key):
        global_container_stack = Application.getInstance().getGlobalContainerStack()
        if global_container_stack:
            if "repetier_id" in global_container_stack.getMetaData():
                global_container_stack.setMetaDataEntry("repetier_id", key)
            else:
                global_container_stack.addMetaDataEntry("repetier_id", key)

        if self._network_plugin:
            # Ensure that the connection states are refreshed.
            self._network_plugin.reCheckConnections()

    @pyqtSlot(result = str)
    def getStoredKey(self):
        global_container_stack = Application.getInstance().getGlobalContainerStack()
        if global_container_stack:
            meta_data = global_container_stack.getMetaData()
            if "repetier_id" in meta_data:
                return global_container_stack.getMetaDataEntry("repetier_id")

        return ""

    @pyqtSlot(str)
    def setApiKey(self, api_key):
        global_container_stack = Application.getInstance().getGlobalContainerStack()
        if global_container_stack:
            if "repetier_api_key" in global_container_stack.getMetaData():
                global_container_stack.setMetaDataEntry("repetier_api_key", api_key)
            else:
                global_container_stack.addMetaDataEntry("repetier_api_key", "e27f74f0-b759-4cfc-9316-c5758f3a387e")

        if self._network_plugin:
            # Ensure that the connection states are refreshed.
            self._network_plugin.reCheckConnections()

    apiKeyChanged = pyqtSignal()

    ##  Get the stored API key of this machine
    #   \return key String containing the key of the machine.
    @pyqtProperty(str, notify = apiKeyChanged)
    def apiKey(self):
        global_container_stack = Application.getInstance().getGlobalContainerStack()
        if global_container_stack:
            Logger.log("d", "APIKEY read %s" % global_container_stack.getMetaDataEntry("repetier_api_key"))
            return global_container_stack.getMetaDataEntry("repetier_api_key")
        else:
            return ""

    selectedInstanceSettingsChanged = pyqtSignal()

    @pyqtProperty(bool, notify = selectedInstanceSettingsChanged)
    def instanceResponded(self):
        return self._instance_responded

    @pyqtProperty(bool, notify = selectedInstanceSettingsChanged)
    def instanceApiKeyAccepted(self):
        return self._instance_api_key_accepted

    @pyqtProperty(bool, notify = selectedInstanceSettingsChanged)
    def instanceSupportsSd(self):
        return self._instance_supports_sd

    @pyqtProperty(bool, notify = selectedInstanceSettingsChanged)
    def instanceSupportsCamera(self):
        return self._instance_supports_camera

    @pyqtSlot(str, str, str)
    def setContainerMetaDataEntry(self, container_id, key, value):
        containers = ContainerRegistry.getInstance().findContainers(None, id = container_id)
        if not containers:
            UM.Logger.log("w", "Could not set metadata of container %s because it was not found.", container_id)
            return False

        container = containers[0]
        if key in container.getMetaData():
            container.setMetaDataEntry(key, value)
        else:
            container.addMetaDataEntry(key, value)

    @pyqtSlot(str)
    def openWebPage(self, url):
        QDesktopServices.openUrl(QUrl(url))

    def _createAdditionalComponentsView(self):
        Logger.log("d", "Creating additional ui components for Repetier-connected printers.")

        path = QUrl.fromLocalFile(os.path.join(os.path.dirname(os.path.abspath(__file__)), "RepetierComponents.qml"))
        self._additional_component = QQmlComponent(Application.getInstance()._engine, path)

        # We need access to engine (although technically we can't)
        self._additional_components_context = QQmlContext(Application.getInstance()._engine.rootContext())
        self._additional_components_context.setContextProperty("manager", self)

        self._additional_components_view = self._additional_component.create(self._additional_components_context)
        if not self._additional_components_view:
            Logger.log("w", "Could not create additional components for Repetier-connected printers.")
            return

        Application.getInstance().addAdditionalComponent("monitorButtons", self._additional_components_view.findChild(QObject, "openRepetierButton"))

    ##  Handler for all requests that have finished.
    def _onRequestFinished(self, reply):

        http_status_code = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
        if not http_status_code:
            # Received no or empty reply
            return

        if reply.operation() == QNetworkAccessManager.GetOperation:
            if "getPrinterConfig" in reply.url().toString():  # Repetier settings dump from getPrinterConfig:			
                if http_status_code == 200:
                    Logger.log("d", "API key accepted by Repetier.")
                    self._instance_api_key_accepted = True

                    try:
                        json_data = json.loads(bytes(reply.readAll()).decode("utf-8"))
                        Logger.log("d",reply.url().toString())
                        Logger.log("d", json_data)
                    except json.decoder.JSONDecodeError:
                        Logger.log("w", "Received invalid JSON from Repetier instance.")
                        json_data = {}

                    if "general" in json_data and "sdcard" in json_data["general"]:
                        self._instance_supports_sd = json_data["general"]["sdcard"]

                    if "webcam" in json_data and "dynamicUrl" in json_data["webcam"]:
                        Logger.log("d", "DiscoverRepetierAction: Checking streamurl")
                        stream_url = json_data["webcam"]["dynamicUrl"]
                        Logger.log("d", "DiscoverRepetierAction: stream_url: %s",stream_url)
                        Logger.log("d", "DiscoverRepetierAction: reply_url: %s",reply.url())
                        if stream_url: #not empty string or None
                            self._instance_supports_camera = True

                elif http_status_code == 401:
                    Logger.log("d", "Invalid API key for Repetier.")
                    self._instance_api_key_accepted = False

                self._instance_responded = True
                self.selectedInstanceSettingsChanged.emit()

    @pyqtSlot(str, str, str, str)
    def testApiKey(self, base_url, api_key, basic_auth_username = "", basic_auth_password = ""):
        self._instance_responded = False
        self._instance_api_key_accepted = False
        self._instance_supports_sd = False
        self._instance_supports_camera = False
        self.selectedInstanceSettingsChanged.emit()
        global_container_stack = Application.getInstance().getGlobalContainerStack()
        if global_container_stack:            
            work_id= global_container_stack.getMetaDataEntry("repetier_id")
        else:
            work_id = "k200"


        if api_key != "":
            Logger.log("d", "Trying to access Repetier instance at %s with the provided API key." % base_url)
            Logger.log("d", "Using %s as API key" % api_key)
            Logger.log("d", "Using %s as work_id" % work_id)

            ## Request 'settings' dump
            url = QUrl(base_url + "printer/api/k200?a=getPrinterConfig&apikey=e27f74f0-b759-4cfc-9316-c5758f3a387e")			
            settings_request = QNetworkRequest(url)
            settings_request.setRawHeader("x-api-key".encode(), api_key.encode())
            settings_request.setRawHeader("User-Agent".encode(), self._user_agent)
            if basic_auth_username and basic_auth_password:
                data = base64.b64encode(("%s:%s" % (basic_auth_username, basic_auth_password)).encode()).decode("utf-8")
                settings_request.setRawHeader("Authorization".encode(), ("Basic %s" % data).encode())
            self._settings_reply = self._manager.get(settings_request)
        else:
            if self._settings_reply:
                self._settings_reply.abort()
                self._settings_reply = None
