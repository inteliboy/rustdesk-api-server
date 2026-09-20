// The token travels in the URL fragment (#token=...), which a browser never
// sends to the server, so it stays out of access logs and Referer headers. It
// is read once and removed from the address bar.
const resetToken = new URLSearchParams(window.location.hash.replace(/^#/, "")).get("token") || "";
if (window.location.hash) window.history.replaceState(null, "", window.location.pathname);

document.getElementById("reset-form").addEventListener("submit", async (evt) => {
  evt.preventDefault();
  const errorEl = document.getElementById("error-message");
  errorEl.classList.add("hidden");
  const password = document.getElementById("password").value;
  if (password !== document.getElementById("confirm").value) {
    errorEl.textContent = "The two passwords are not the same.";
    errorEl.classList.remove("hidden");
    return;
  }
  if (!resetToken) {
    errorEl.textContent = "This page needs the full link the administrator gave you.";
    errorEl.classList.remove("hidden");
    return;
  }
  try {
    await api("/api/v1/auth/reset-password", { method: "POST", body: JSON.stringify({ token: resetToken, password }) });
    window.location.href = "/login?reset=1";
  } catch (err) {
    errorEl.textContent = err.message || "Could not set the password.";
    errorEl.classList.remove("hidden");
  }
});
