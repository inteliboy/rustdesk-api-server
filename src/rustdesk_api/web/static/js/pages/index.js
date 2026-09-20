(async () => {
  try {
    const status = await api("/api/v1/auth/setup/status");
    if (status.setup_required) {
      window.location.href = "/setup";
      return;
    }
  } catch (e) {}
  try {
    await api("/api/v1/auth/me");
    window.location.href = "/dashboard";
  } catch (e) {
    window.location.href = "/login";
  }
})();
