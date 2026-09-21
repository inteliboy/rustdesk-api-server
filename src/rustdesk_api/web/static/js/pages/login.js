(async () => {
  try {
    const status = await api("/api/v1/auth/setup/status");
    if (status.setup_required) {
      window.location.href = "/setup";
    }
  } catch (e) {}
})();

(async () => {
  if (new URLSearchParams(window.location.search).get("reset") === "1") {
    const notice = document.getElementById("login-notice");
    notice.textContent = "Your password was changed. Sign in with the new one.";
    notice.classList.remove("hidden");
  }
  if (new URLSearchParams(window.location.search).get("sso") === "unavailable") {
    const notice = document.getElementById("login-notice");
    notice.textContent = "Single sign-on is not available right now. Sign in with your password, or try again.";
    notice.classList.remove("hidden");
  }
  try {
    const options = await api("/api/v1/auth/options");
    if (options.oidc_name) {
      document.getElementById("sso-name").textContent = options.oidc_name;
      document.getElementById("sso-block").classList.remove("hidden");
    }
    if (options.registration_enabled) {
      document.getElementById("register-link").classList.remove("hidden");
      document.getElementById("register-sep").classList.remove("hidden");
    }
  } catch (e) {}
})();

// The challenge from the password step, when the account has two-factor
// authentication: it is what the code has to be presented with.
let twoFactorChallenge = null;

function finishLogin() {
  const params = new URLSearchParams(window.location.search);
  window.location.href = safeNextPath(params.get("next"));
}

function showCodeStep(show) {
  document.getElementById("login-form").classList.toggle("hidden", show);
  document.getElementById("code-form").classList.toggle("hidden", !show);
  if (show) document.getElementById("code").focus();
}

document.getElementById("login-form").addEventListener("submit", async (evt) => {
  evt.preventDefault();
  const errorEl = document.getElementById("error-message");
  errorEl.classList.add("hidden");
  const username = document.getElementById("username").value;
  const password = document.getElementById("password").value;
  try {
    const result = await api("/api/v1/auth/login", { method: "POST", body: JSON.stringify({ username, password }) });
    if (result.two_factor_required) {
      twoFactorChallenge = result.challenge;
      document.getElementById("code").value = "";
      document.getElementById("code-error").classList.add("hidden");
      showCodeStep(true);
      return;
    }
    finishLogin();
  } catch (err) {
    errorEl.textContent = err.message || "Sign in failed.";
    errorEl.classList.remove("hidden");
  }
});

document.getElementById("code-form").addEventListener("submit", async (evt) => {
  evt.preventDefault();
  const errorEl = document.getElementById("code-error");
  errorEl.classList.add("hidden");
  try {
    await api("/api/v1/auth/login/2fa", {
      method: "POST",
      body: JSON.stringify({ challenge: twoFactorChallenge, code: document.getElementById("code").value }),
    });
    finishLogin();
  } catch (err) {
    errorEl.textContent = err.message || "Verification failed.";
    errorEl.classList.remove("hidden");
  }
});

document.getElementById("code-back").addEventListener("click", () => {
  twoFactorChallenge = null;
  document.getElementById("password").value = "";
  showCodeStep(false);
});
