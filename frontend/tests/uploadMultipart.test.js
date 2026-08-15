import test from "node:test";
import assert from "node:assert/strict";

import { uploadMultipart } from "../src/api.js";

class FakeEvents {
  constructor() {
    this.listeners = new Map();
  }

  addEventListener(name, callback) {
    this.listeners.set(name, callback);
  }

  emit(name, event = {}) {
    this.listeners.get(name)?.(event);
  }
}

class FakeXmlHttpRequest extends FakeEvents {
  constructor() {
    super();
    this.upload = new FakeEvents();
    this.status = 201;
    this.responseText = '{"ok":true}';
    this.headers = new Map();
  }

  open(method, path) {
    this.method = method;
    this.path = path;
  }

  setRequestHeader(name, value) {
    this.headers.set(name, value);
  }

  send(body) {
    this.body = body;
    this.upload.emit("progress", {
      lengthComputable: true,
      loaded: 75,
      total: 100,
    });
    this.upload.emit("load");
    this.emit("load");
  }
}

test("multipart upload reports byte progress before server processing", async () => {
  const original = globalThis.XMLHttpRequest;
  globalThis.XMLHttpRequest = FakeXmlHttpRequest;
  const events = [];
  try {
    const response = await uploadMultipart("/synthetic", new FormData(), {
      onProgress: ({ percent }) => events.push(`progress:${percent}`),
      onUploadComplete: () => events.push("upload-complete"),
    });

    assert.deepEqual(response, { ok: true });
    assert.deepEqual(events, [
      "progress:75",
      "upload-complete",
      "progress:100",
    ]);
  } finally {
    globalThis.XMLHttpRequest = original;
  }
});
