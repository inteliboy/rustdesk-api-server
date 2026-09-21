// Starts the browser web client for one device: asks the server for a session (it
// checks that this user may control that device and sets a short-lived ticket
// cookie for the WebSocket bridge), hands the answer to the client as
// window.__RD__, then loads the client itself.
// The client is a separate program (/static/webclient/, source in /webclient); this
// file is only the launcher. The device password is typed into the client's own
// screen and goes to the device end to end - never to this server.

(async function launch() {
  const version = document.currentScript ? document.currentScript.dataset.version || "" : "";
  const launcher = document.getElementById("rd-launch");
  const text = document.getElementById("rd-launch-text");
  const error = document.getElementById("rd-launch-error");
  const back = document.getElementById("rd-launch-back");

  function fail(message) {
    text.hidden = true;
    error.textContent = message;
    error.hidden = false;
    back.hidden = false;
  }

  const deviceId = Number(new URLSearchParams(location.search).get("device"));
  if (!Number.isInteger(deviceId) || deviceId < 1) {
    fail(t("Open the web client from a device: use “Open in browser” on the device page."));
    return;
  }

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
    workerUrl: `/static/webclient/session.worker.js?v=${encodeURIComponent(version)}`,
  };
  try {
    await import(`/static/webclient/app.js?v=${encodeURIComponent(version)}`);
  } catch (err) {
    fail(t("The web client could not be loaded: {1}", [err.message]));
    return;
  }
  launcher.hidden = true;
})();
