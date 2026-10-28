"""The plugin's two viewers that run on their own: her screens and her webcam."""
from plugins.flashhead import camera, screens


def start(plugin_loader, settings):
    screens.start(plugin_loader, settings)
    camera.start(plugin_loader, settings)


def stop():
    screens.stop()
    camera.stop()


def on_settings_saved(settings):
    screens.on_settings_saved(settings)
    camera.on_settings_saved(settings)
