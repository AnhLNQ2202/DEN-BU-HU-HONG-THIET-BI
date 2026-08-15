from __future__ import annotations

import re
import shutil
import struct
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADDIN = ROOT / "src" / "asset_compensation" / "integrations" / "outlook_addin"
MANIFEST = ADDIN / "manifest.xml.template"


def test_outlook_addin_assets_are_complete() -> None:
    expected = {
        "__init__.py",
        "README.md",
        "logo.png",
        "logo-16.png",
        "logo-32.png",
        "logo-64.png",
        "logo-80.png",
        "logo-128.png",
        "manifest.xml.template",
        "taskpane.css",
        "taskpane.html",
        "taskpane.js",
    }
    assert expected <= {path.name for path in ADDIN.iterdir()}


def test_manifest_uses_read_item_and_https_origin_template() -> None:
    raw = MANIFEST.read_text(encoding="utf-8")
    assert raw.count("__ASSET_HUB_ORIGIN__") >= 8
    assert "<Permissions>ReadItem</Permissions>" in raw
    assert "ReadWriteItem" not in raw
    assert 'Name="Mailbox" MinVersion="1.14"' in raw
    assert 'DefaultMinVersion="1.14"' in raw
    assert "MessageReadCommandSurface" in raw
    assert "/outlook-addin/logo-64.png" in raw
    assert "/outlook-addin/logo-128.png" in raw
    for size in (16, 32, 80):
        assert f'/outlook-addin/logo-{size}.png"' in raw

    rendered = raw.replace("__ASSET_HUB_ORIGIN__", "https://staging.example.test")
    root = ET.fromstring(rendered)
    assert root.attrib["{http://www.w3.org/2001/XMLSchema-instance}type"] == "MailApp"
    for value in re.findall(r'https://[^<"]+', rendered):
        assert value.startswith("https://")


def test_manifest_icons_have_the_exact_required_square_dimensions() -> None:
    for size in (16, 32, 64, 80, 128):
        payload = (ADDIN / f"logo-{size}.png").read_bytes()
        assert payload[:8] == b"\x89PNG\r\n\x1a\n"
        width, height = struct.unpack(">II", payload[16:24])
        assert (width, height) == (size, size)


def test_taskpane_loads_only_official_office_script_and_local_script() -> None:
    html = (ADDIN / "taskpane.html").read_text(encoding="utf-8")
    script_sources = re.findall(r'<script[^>]+src="([^"]+)"', html)
    assert script_sources == [
        "https://appsforoffice.microsoft.com/lib/1/hosted/office.js",
        "./taskpane.js",
    ]
    assert not re.search(r"<script(?![^>]+src=)[^>]*>", html)
    assert not re.search(r"\sstyle=", html)
    assert 'rel="noreferrer"' in html


def test_taskpane_enforces_companion_safety_contract() -> None:
    javascript = (ADDIN / "taskpane.js").read_text(encoding="utf-8")
    assert 'exchange: "/api/companion/exchange"' in javascript
    assert 'uploadEmail: "/api/companion/client/emails"' in javascript
    assert 'draftPackages: "/api/companion/client/draft-packages"' in javascript
    assert 'uploadIntent: "companion-email-v1"' in javascript
    assert 'acknowledgeIntent: "companion-ack-v1"' in javascript
    assert "getAsFileAsync" in javascript
    assert "displayReplyAllFormAsync" in javascript
    assert "source_eml_handle !== expectedSourceHandle" in javascript
    assert "sessionStorage" in javascript
    assert "localStorage" not in javascript
    assert ".send(" not in javascript
    assert 'isMailboxSetSupported("1.14")' in javascript
    assert 'isMailboxSetSupported("1.15")' in javascript
    assert "base64file" in javascript
    assert "credentials: \"omit\"" in javascript
    assert "emailBytes: 2 * 1024 * 1024" in javascript
    assert "draftPackageResponseCharacters: 37 * 1024 * 1024" in javascript
    assert "const operationItemKey = state.itemKey" in javascript
    assert "storeItemSourceMapping(operationItemKey, handle)" in javascript
    assert "requireCurrentItemBinding(operationItemKey, operationArtifactHandle)" in javascript
    assert "const currentItem = requireCurrentItemBinding" in javascript


def test_taskpane_binding_guard_rejects_an_item_switch() -> None:
    node = shutil.which("node")
    if node is None:
        return
    script = ADDIN / "taskpane.js"
    harness = r"""
const fs = require("fs");
const vm = require("vm");
const values = new Map();
const sessionStorage = {
  getItem(key) { return values.has(key) ? values.get(key) : null; },
  setItem(key, value) { values.set(key, String(value)); },
  removeItem(key) { values.delete(key); },
};
const context = {
  console,
  Blob,
  Uint8Array,
  JSON,
  Date,
  sessionStorage,
  document: { addEventListener() {}, hidden: false },
  window: { setTimeout, clearTimeout, setInterval, clearInterval, atob },
  Office: { context: { mailbox: { item: { itemId: "mail-A" } } } },
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1], "utf8"), context);
vm.runInContext(`
  const handle = "eml-sha256-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
  state.itemKey = "mail-A";
  state.artifactHandle = handle;
  storeItemSourceMapping("mail-A", handle);
  if (!isCurrentItemBinding("mail-A", handle)) throw new Error("initial binding failed");
  Office.context.mailbox.item = { itemId: "mail-B" };
  if (isCurrentItemBinding("mail-A", handle)) throw new Error("item switch was accepted");
  let rejected = false;
  try { requireCurrentItemBinding("mail-A", handle); } catch { rejected = true; }
  if (!rejected) throw new Error("item switch did not fail closed");
  state.itemKey = "mail-B";
  storeItemSourceMapping("mail-B", handle);
  if (!isHandleAmbiguous(handle)) throw new Error("duplicate handle was not ambiguous");
  if (uniqueMappedHandleForItem("mail-A") !== null) {
    throw new Error("first duplicate stayed usable");
  }
  if (uniqueMappedHandleForItem("mail-B") !== null) {
    throw new Error("second duplicate stayed usable");
  }
`, context);
"""
    subprocess.run(
        [node, "-e", harness, str(script)],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
