(async () => {
  try {
    const status = await api("/api/v1/auth/setup/status");
    if (!status.setup_required) {
      window.location.href = "/login";
    }
  } catch (e) {}
})();

document.getElementById("setup-form").addEventListener("submit", async (evt) => {
  evt.preventDefault();
  const errorEl = document.getElementById("error-message");
  errorEl.classList.add("hidden");
  const username = document.getElementById("username").value;
  const email = document.getElementById("email").value || null;
  const password = document.getElementById("password").value;
  try {
    await api("/api/v1/auth/setup", { method: "POST", body: JSON.stringify({ username, email, password }) });
    await api("/api/v1/auth/login", { method: "POST", body: JSON.stringify({ username, password }) });
    window.location.href = "/dashboard";
  } catch (err) {
    errorEl.textContent = err.message || "Setup failed.";
    errorEl.classList.remove("hidden");
  }
});
