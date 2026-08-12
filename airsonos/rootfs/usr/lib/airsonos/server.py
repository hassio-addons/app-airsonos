#!/usr/bin/env python3
"""Small Ingress web UI to toggle the visibility of AirSonos devices.

It reads the AirConnect configuration file (``/config/airsonos.xml``),
lists all discovered devices and allows enabling or disabling each of
them. Disabled devices are no longer exposed as AirPlay receivers.

The server intentionally only uses the Python standard library so no
extra dependencies need to be installed.
"""

from __future__ import annotations

import html
import os
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

CONFIG_FILE = os.environ.get("AIRSONOS_CONFIG", "/config/airsonos.xml")
LISTEN_PORT = int(os.environ.get("AIRSONOS_WEB_PORT", "8099"))
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")


def read_devices():
    """Return a list of ``(udn, name, enabled)`` tuples from the config."""
    if not os.path.exists(CONFIG_FILE):
        return []

    try:
        tree = ET.parse(CONFIG_FILE)
    except ET.ParseError:
        return []

    devices = []
    for device in tree.getroot().findall("device"):
        udn = (device.findtext("udn") or "").strip()
        if not udn:
            continue
        name = (device.findtext("name") or "").strip() or udn
        enabled_text = (device.findtext("enabled") or "1").strip()
        enabled = enabled_text not in ("0", "false", "no", "")
        devices.append((udn, name, enabled))

    devices.sort(key=lambda item: item[1].lower())
    return devices


def write_devices(enabled_udns):
    """Persist the enabled state for every device in the config file.

    ``enabled_udns`` is the set of device UDNs that should stay visible;
    every other known device is disabled.
    """
    tree = ET.parse(CONFIG_FILE)
    root = tree.getroot()

    for device in root.findall("device"):
        udn = (device.findtext("udn") or "").strip()
        if not udn:
            continue

        enabled_el = device.find("enabled")
        if enabled_el is None:
            enabled_el = ET.SubElement(device, "enabled")
        enabled_el.text = "1" if udn in enabled_udns else "0"

    tree.write(CONFIG_FILE, encoding="utf-8", xml_declaration=True)


def restart_addon():
    """Restart this add-on through the Supervisor so changes take effect."""
    if not SUPERVISOR_TOKEN:
        return

    # Give the browser time to receive the response before we go down.
    time.sleep(1)

    request = urllib.request.Request(
        "http://supervisor/addons/self/restart",
        method="POST",
        headers={"Authorization": "Bearer " + SUPERVISOR_TOKEN},
    )
    try:
        urllib.request.urlopen(request, timeout=30)
    except urllib.error.URLError:
        pass


PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AirSonos devices</title>
<style>
  body {{ font-family: sans-serif; margin: 0; padding: 16px;
         background: #fafafa; color: #212121; }}
  h1 {{ font-size: 1.4rem; }}
  p.hint {{ color: #616161; }}
  ul {{ list-style: none; padding: 0; max-width: 640px; }}
  li {{ background: #fff; border: 1px solid #e0e0e0; border-radius: 6px;
        padding: 12px 16px; margin-bottom: 8px; display: flex;
        align-items: center; }}
  li label {{ display: flex; align-items: center; width: 100%;
              cursor: pointer; }}
  li input {{ width: 20px; height: 20px; margin-right: 12px; }}
  .name {{ font-weight: 600; }}
  .udn {{ display: block; color: #9e9e9e; font-size: 0.75rem; }}
  button {{ background: #03a9f4; color: #fff; border: none; border-radius: 6px;
            padding: 12px 20px; font-size: 1rem; cursor: pointer; }}
  button:hover {{ background: #0288d1; }}
  .empty {{ color: #616161; max-width: 640px; }}
</style>
</head>
<body>
<h1>AirSonos devices</h1>
{body}
</body>
</html>
"""


def render_index(saved=False):
    devices = read_devices()

    notice = ""
    if saved:
        notice = (
            '<p class="hint">Changes saved. The add-on is restarting to apply '
            "them; this page will be unavailable for a moment.</p>"
        )

    if not devices:
        body = notice + (
            '<p class="empty">No devices have been detected yet. Make sure the '
            "add-on has been running for a bit so it can discover your Sonos / "
            "UPnP players, then refresh this page.</p>"
        )
        return PAGE.format(body=body)

    items = []
    for udn, name, enabled in devices:
        checked = " checked" if enabled else ""
        items.append(
            "<li><label>"
            '<input type="checkbox" name="device" value="{udn}"{checked}>'
            '<span><span class="name">{name}</span>'
            '<span class="udn">{udn}</span></span>'
            "</label></li>".format(
                udn=html.escape(udn), name=html.escape(name), checked=checked
            )
        )

    body = (
        notice
        + '<p class="hint">Uncheck a device to hide it from AirPlay. '
        "Saving restarts the add-on to apply the changes.</p>"
        + '<form method="post" action="">'
        + "<ul>" + "".join(items) + "</ul>"
        + "<button type=\"submit\">Save</button>"
        + "</form>"
    )
    return PAGE.format(body=body)


class Handler(BaseHTTPRequestHandler):
    def _send_html(self, content, status=200):
        payload = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self._send_html(render_index())

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        params = parse_qs(raw)
        enabled_udns = set(params.get("device", []))

        try:
            write_devices(enabled_udns)
        except (ET.ParseError, OSError):
            self._send_html(
                PAGE.format(
                    body='<p class="empty">Could not update the configuration '
                    "file.</p>"
                ),
                status=500,
            )
            return

        self._send_html(render_index(saved=True))
        threading.Thread(target=restart_addon, daemon=True).start()

    def log_message(self, *args):  # noqa: D401 - silence default logging
        return


def main():
    server = ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
