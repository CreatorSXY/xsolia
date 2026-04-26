(function () {
  const API_BASE =
    window.KROTKA_API_BASE ||
    (location.hostname === "localhost" || location.hostname === "127.0.0.1"
      ? "http://127.0.0.1:8000"
      : "https://api.xsolia.com");
  const GSAP_CORE_URL = "https://cdn.jsdelivr.net/npm/gsap@3/dist/gsap.min.js";
  const GSAP_SCROLLTRIGGER_URL = "https://cdn.jsdelivr.net/npm/gsap@3/dist/ScrollTrigger.min.js";

  function getToken() {
    return localStorage.getItem("access_token") || "";
  }

  function getSession() {
    return {
      userId: localStorage.getItem("user_id"),
      role: localStorage.getItem("role"),
      name: localStorage.getItem("name"),
      subscription: localStorage.getItem("subscription"),
      username: localStorage.getItem("username"),
      avatarUrl: localStorage.getItem("avatar_url"),
      accessToken: getToken(),
    };
  }

  function setSession(payload) {
    if (payload.user_id != null) localStorage.setItem("user_id", String(payload.user_id));
    if (payload.role) localStorage.setItem("role", payload.role);
    if (payload.name) localStorage.setItem("name", payload.name);
    if (payload.subscription) localStorage.setItem("subscription", payload.subscription);
    if (payload.username != null) {
      localStorage.setItem("username", payload.username);
    } else if ("username" in payload) {
      localStorage.removeItem("username");
    }
    if (payload.avatar_url != null) {
      localStorage.setItem("avatar_url", payload.avatar_url);
    } else if ("avatar_url" in payload) {
      localStorage.removeItem("avatar_url");
    }
    if (payload.access_token) localStorage.setItem("access_token", payload.access_token);
  }

  function clearSession() {
    localStorage.removeItem("user_id");
    localStorage.removeItem("role");
    localStorage.removeItem("name");
    localStorage.removeItem("subscription");
    localStorage.removeItem("username");
    localStorage.removeItem("avatar_url");
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

    if (response.status === 401 && normalized.auth) {
      clearSession();
      if (!normalized.disableAuthRedirect) {
        window.location.href = "login.html?expired=1";
      }
    }

    return { response, data };
  }

  function formatApiError(data, fallback) {
    const detail = data && data.detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      return detail
        .map((item) => {
          if (typeof item === "string") return item;
          if (item && typeof item.msg === "string") return item.msg;
          return "";
        })
        .filter(Boolean)
        .join(" · ") || fallback || "Something went wrong, please try again.";
    }
    if (detail && typeof detail === "object") {
      return Object.values(detail).filter(Boolean).join(" · ") || fallback || "Something went wrong, please try again.";
    }
    return fallback || "Something went wrong, please try again.";
  }

  function getFieldErrors(data) {
    return data && data.fields && typeof data.fields === "object" ? data.fields : {};
  }

  function prefersReducedMotion() {
    return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  }

  function getRevealItems(root) {
    const scope = root || document;
    return Array.from(scope.querySelectorAll("[data-reveal], .motion-reveal")).filter(
      (item) => !item.dataset.motionBound
    );
  }

  function revealWithoutGsap(root) {
    const items = getRevealItems(root);
    if (!items.length) return;

    if (!("IntersectionObserver" in window)) {
      items.forEach((item) => {
        item.dataset.motionBound = "fallback";
        item.classList.add("is-visible");
      });
      return;
    }

    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (!entry.isIntersecting) return;
          entry.target.classList.add("is-visible");
          observer.unobserve(entry.target);
        });
      },
      { threshold: 0.14 }
    );

    items.forEach((item, index) => {
      item.dataset.motionBound = "fallback";
      if (!item.style.getPropertyValue("--reveal-delay")) {
        item.style.setProperty("--reveal-delay", `${Math.min(index * 55, 330)}ms`);
      }
      observer.observe(item);
    });
  }

  let gsapBaseInitialized = false;
  let gsapLoadPromise = null;

  function loadScript(src, globalCheck) {
    if (globalCheck()) return Promise.resolve();

    return new Promise((resolve, reject) => {
      const existing = document.querySelector(`script[src="${src}"]`);
      if (existing) {
        existing.addEventListener("load", resolve, { once: true });
        existing.addEventListener("error", reject, { once: true });
        return;
      }

      const script = document.createElement("script");
      script.src = src;
      script.async = true;
      script.onload = resolve;
      script.onerror = reject;
      document.head.appendChild(script);
    });
  }

  function loadGsap() {
    if (window.gsap && window.ScrollTrigger) return Promise.resolve();
    if (!gsapLoadPromise) {
      gsapLoadPromise = loadScript(GSAP_CORE_URL, () => Boolean(window.gsap))
        .then(() => loadScript(GSAP_SCROLLTRIGGER_URL, () => Boolean(window.ScrollTrigger)));
    }
    return gsapLoadPromise;
  }

  function initHeroMotion(gsap, ScrollTrigger) {
    const hero = document.querySelector(".landing-hero");
    if (!hero) return;

    const heroItems = [".brand-line", ".hero-title", ".hero-sub", ".hero-cta-row", ".hero-note"]
      .map((selector) => document.querySelector(selector))
      .filter(Boolean);

    if (heroItems.length) {
      gsap.set(heroItems, { autoAlpha: 0, y: 28 });
      gsap.timeline({ defaults: { duration: 0.78, ease: "power3.out" } })
        .to(heroItems, {
          autoAlpha: 1,
          y: 0,
          stagger: 0.075,
          clearProps: "transform,opacity,visibility",
        });
    }

    gsap.to(".landing-inner", {
      yPercent: -7,
      autoAlpha: 0.92,
      ease: "none",
      scrollTrigger: {
        trigger: hero,
        start: "top top",
        end: "bottom top",
        scrub: 0.8,
      },
    });
  }

  function initAmbientMotion(gsap) {
    const orbitItems = gsap.utils.toArray(".insight-orbit span");
    if (orbitItems.length) {
      gsap.to(orbitItems, {
        y: (index) => (index % 2 === 0 ? -10 : 8),
        rotation: (index) => (index % 2 === 0 ? 2.5 : -2),
        duration: 4.8,
        ease: "sine.inOut",
        stagger: 0.28,
        repeat: -1,
        yoyo: true,
        overwrite: "auto",
      });
    }

    const finalCta = document.querySelector(".final-cta");
    if (finalCta) {
      gsap.fromTo(
        finalCta,
        { scale: 0.985 },
        {
          scale: 1,
          duration: 0.85,
          ease: "power2.out",
          scrollTrigger: {
            trigger: finalCta,
            start: "top 82%",
            once: true,
          },
        }
      );
    }
  }

  function initGsapMotion(root) {
    const gsap = window.gsap;
    const ScrollTrigger = window.ScrollTrigger;

    if (!gsap || !ScrollTrigger || prefersReducedMotion()) {
      revealWithoutGsap(root);
      return;
    }

    document.documentElement.classList.add("gsap-motion");
    gsap.registerPlugin(ScrollTrigger);
    gsap.defaults({ duration: 0.68, ease: "power3.out" });

    const items = getRevealItems(root);
    if (items.length) {
      const clampDelay = gsap.utils.clamp(0, 0.32);
      items.forEach((item, index) => {
        item.dataset.motionBound = "gsap";
        item.classList.add("is-visible");
        item.style.setProperty("--reveal-delay", `${clampDelay(index * 0.055)}s`);
      });

      gsap.set(items, { autoAlpha: 0, y: 22 });
      ScrollTrigger.batch(items, {
        start: "top 88%",
        once: true,
        interval: 0.08,
        batchMax: 6,
        onEnter: (batch) => {
          gsap.to(batch, {
            autoAlpha: 1,
            y: 0,
            stagger: 0.07,
            overwrite: "auto",
            clearProps: "transform,opacity,visibility",
          });
        },
      });
    }

    if (!gsapBaseInitialized) {
      gsapBaseInitialized = true;
      initHeroMotion(gsap, ScrollTrigger);
      initAmbientMotion(gsap);
    }

    ScrollTrigger.refresh();
  }

  function initMotion(root) {
    const scope = root || document;

    if (prefersReducedMotion()) {
      revealWithoutGsap(scope);
      return;
    }

    if (window.gsap && window.ScrollTrigger) {
      initGsapMotion(scope);
      return;
    }

    let fallbackUsed = false;
    const fallbackTimer = window.setTimeout(() => {
      fallbackUsed = true;
      revealWithoutGsap(scope);
    }, 1200);

    loadGsap()
      .then(() => {
        window.clearTimeout(fallbackTimer);
        if (!fallbackUsed) {
          initGsapMotion(scope);
        }
      })
      .catch(() => {
        window.clearTimeout(fallbackTimer);
        revealWithoutGsap(scope);
      });
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
    formatApiError,
    getFieldErrors,
    initMotion,
    requireAuth,
  };

  window.XsoliaApp = appApi;

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initMotion);
  } else {
    initMotion();
  }
})();
