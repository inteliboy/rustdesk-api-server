// Starts the browser web client for one device. First the server checks that it can get a session through
// to hbbs and hbbr and asks hbbs the question the client will ask (the client itself only says "ID server
// connection lost"); a problem is shown here, in words, with a way to go on anyway. Then the server gives a
// session (it checks that this user may control that device and sets a short-lived ticket cookie for the
// WebSocket bridge), the answer becomes window.__RD__, and the client itself is loaded.
// The client is a separate program (/static/webclient/, source in /webclient); this file is only the
// launcher. The device password is typed into the client's own screen and goes to the device end to end -
// never to this server.

(async function launch() {
  const version = document.currentScript ? document.currentScript.dataset.version || "" : "";
  const sourceUrl = document.currentScript ? document.currentScript.dataset.source || "" : "";
  const launcher = document.getElementById("rd-launch");
  const text = document.getElementById("rd-launch-text");
  const error = document.getElementById("rd-launch-error");
  const detail = document.getElementById("rd-launch-detail");
  const back = document.getElementById("rd-launch-back");
  const anyway = document.getElementById("rd-launch-anyway");

  function fail(message) {
    text.hidden = true;
    error.textContent = message;
    error.hidden = false;
    back.hidden = false;
  }

  // What the administrator's check found, line by line (technical, so not translated).
  function detailOf(check) {
    const lines = [];
    for (const [label, target] of [["ID server (hbbs)", check.hbbs], ["Relay (hbbr)", check.hbbr]]) {
      if (target) lines.push(`${label}: ${target.where} - ${target.ok ? "a WebSocket opens" : target.error}`);
    }
    if (check.answer) lines.push(`hbbs's answer to the request for this device: ${check.answer}`);
    if (check.last_problem) lines.push(`Last problem the bridge logged: ${check.last_problem}`);
    return lines.join("\n");
  }

  const deviceId = Number(new URLSearchParams(location.search).get("device"));
  if (!Number.isInteger(deviceId) || deviceId < 1) {
    fail(t("Open the web client from a device: click its ID and choose “Open in browser”."));
    return;
  }

  async function begin() {
    let session;
    try {
      session = await api("/api/v1/webclient/sessions", {
        method: "POST",
        body: JSON.stringify({ device_id: deviceId }),
      });
    } catch (err) {
      if (err.status === 401) {
        window.location.href = "/login";
        return;
      }
      fail(err.message);
      return;
    }

    const scheme = location.protocol === "https:" ? "wss:" : "ws:";
    window.__RD__ = {
      peerId: session.peer_id,
      serverKeyB64: session.key,
      wsIdUrl: `${scheme}//${location.host}${session.ws_id_path}`,
      wsRelayUrl: `${scheme}//${location.host}${session.ws_relay_path}`,
      myId: session.my_id,
      myName: session.my_name,
      version: session.version,
      sourceUrl,
      workerUrl: `/static/webclient/session.worker.js?v=${encodeURIComponent(version)}`,
    };
    try {
      await import(`/static/webclient/app.js?v=${encodeURIComponent(version)}`);
    } catch (err) {
      fail(t("The web client could not be loaded: {1}", [err.message]));
      return;
    }
    launcher.hidden = true;
  }

  text.textContent = t("Checking the connection to the RustDesk servers...");
  let check = null;
  try {
    check = await api(`/api/v1/webclient/check?device_id=${deviceId}`);
  } catch (err) {
    if (err.status === 401) {
      window.location.href = "/login";
      return;
    }
    // Not enabled, not set up, or not this user's device: the reason is the answer. Anything else (the check
    // itself could not run) must not stop the client from trying.
    if ([403, 404, 409].includes(err.status)) {
      fail(err.message);
      return;
    }
  }

  if (check && !check.ok) {
    text.hidden = true;
    error.textContent = check.message;
    error.hidden = false;
    const lines = detailOf(check);
    if (lines) {
      detail.textContent = lines;
      detail.hidden = false;
    }
    back.hidden = false;
    anyway.hidden = false;
    anyway.addEventListener("click", () => {
      anyway.hidden = true;
      error.hidden = true;
      detail.hidden = true;
      back.hidden = true;
      text.hidden = false;
      text.textContent = t("Opening the web client...");
      begin();
    });
    return;
  }

  text.textContent = t("Opening the web client...");
  await begin();
})();
