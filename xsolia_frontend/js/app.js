(function () {
  const API_BASE = "https://api.xsolia.com";

  function getToken() {
    return localStorage.getItem("access_token") || "";
  }

  function getSession() {
    return {
      userId: localStorage.getItem("user_id"),
      role: localStorage.getItem("role"),
      name: localStorage.getItem("name"),
      subscription: localStorage.getItem("subscription"),
      accessToken: getToken(),
    };
  }

  function setSession(payload) {
    if (payload.user_id != null) localStorage.setItem("user_id", String(payload.user_id));
    if (payload.role) localStorage.setItem("role", payload.role);
    if (payload.name) localStorage.setItem("name", payload.name);
    if (payload.subscription) localStorage.setItem("subscription", payload.subscription);
    if (payload.access_token) localStorage.setItem("access_token", payload.access_token);
  }

  function clearSession() {
    localStorage.removeItem("user_id");
    localStorage.removeItem("role");
    localStorage.removeItem("name");
    localStorage.removeItem("subscription");
    localStorage.removeItem("access_token");
  }

  function buildHeaders(options) {
    const headers = Object.assign({}, options && options.headers ? options.headers : {});

    if (options && options.auth) {
      const token = getToken();
      if (token) {
        headers.Authorization = `Bearer ${token}`;
      }
    }

    if (options && options.body !== undefined && !headers["Content-Type"]) {
      headers["Content-Type"] = "application/json";
    }

    return headers;
  }

  async function apiFetch(path, options) {
    const normalized = options || {};
    const fetchOptions = {
      method: normalized.method || "GET",
      headers: buildHeaders(normalized),
    };

    if (normalized.body !== undefined) {
      fetchOptions.body = JSON.stringify(normalized.body);
    }

    const response = await fetch(`${API_BASE}${path}`, fetchOptions);

    let data = null;
    const contentType = response.headers.get("content-type") || "";
    if (contentType.includes("application/json")) {
      data = await response.json();
    } else {
      data = await response.text();
    }

    return { response, data };
  }

  function requireAuth(config) {
    const settings = config || {};
    const session = getSession();

    if (!session.userId || !session.accessToken) {
      window.location.href = settings.redirect || "login.html";
      return null;
    }

    if (settings.role && session.role !== settings.role) {
      window.location.href = settings.redirect || "login.html";
      return null;
    }

    return session;
  }

  const appApi = {
    API_BASE,
    getSession,
    setSession,
    clearSession,
    apiFetch,
    requireAuth,
  };

  window.XsoliaApp = appApi;
})();
