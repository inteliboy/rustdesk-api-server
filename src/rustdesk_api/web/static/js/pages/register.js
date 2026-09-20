(async () => {
  try {
    const options = await api("/api/v1/auth/options");
    if (!options.registration_enabled) {
      window.location.href = "/login";
      return;
    }
    document.getElementById("register-note").textContent = options.registration_requires_approval
      ? "An administrator has to approve a new account before it can sign in."
      : "You can sign in as soon as the account is created.";
  } catch (e) {}
})();

document.getElementById("register-form").addEventListener("submit", async (evt) => {
  evt.preventDefault();
  const errorEl = document.getElementById("error-message");
  errorEl.classList.add("hidden");
  const body = {
    username: document.getElementById("username").value,
    email: document.getElementById("email").value || null,
    password: document.getElementById("password").value,
  };
  try {
    const result = await api("/api/v1/auth/register", { method: "POST", body: JSON.stringify(body) });
    document.getElementById("register-form").classList.add("hidden");
    document.getElementById("register-done").classList.remove("hidden");
    document.getElementById("register-done-text").textContent =
      result.status === "pending_approval"
        ? "Your account was created. It can sign in once an administrator has approved it."
        : "Your account was created. You can sign in now.";
  } catch (err) {
    errorEl.textContent = err.message || "Registration failed.";
    errorEl.classList.remove("hidden");
  }
});
