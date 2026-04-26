(function () {
  const LOGO_PATHS = {
    navMarkSvg: 'assets/logo/logo-mark.svg',
    navMarkPng: 'assets/logo/logo-mark.png',
    faviconSvg: 'assets/logo/logo-mark.svg',
    faviconPng: 'assets/logo/logo-mark.png'
  };

  function buildNavLogo() {
    const picture = document.createElement('picture');
    picture.className = 'site-logo-mark';

    const source = document.createElement('source');
    source.setAttribute('srcset', LOGO_PATHS.navMarkSvg);
    source.setAttribute('type', 'image/svg+xml');

    const img = document.createElement('img');
    img.className = 'site-logo-mark-img';
    img.src = LOGO_PATHS.navMarkPng;
    img.alt = 'xsolia logo';
    img.width = 32;
    img.height = 32;
    img.decoding = 'async';

    picture.appendChild(source);
    picture.appendChild(img);
    return picture;
  }

  function replaceNavLogoPlaceholder() {
    const brandLinks = document.querySelectorAll('.x-nav a[href="index.html"]');
    if (!brandLinks.length) return;

    brandLinks.forEach((brandLink) => {
      if (brandLink.querySelector('.site-logo-mark')) return;

      const firstElement = brandLink.firstElementChild;
      if (!firstElement) return;

      const navLogo = buildNavLogo();
      brandLink.replaceChild(navLogo, firstElement);
    });
  }

  function upsertFavicon(selector, attrs) {
    let link = document.head.querySelector(selector);

    if (!link) {
      link = document.createElement('link');
      document.head.appendChild(link);
    }

    Object.entries(attrs).forEach(([key, value]) => {
      link.setAttribute(key, value);
    });
  }

  function ensureFavicons() {
    upsertFavicon('link[data-xsolia-favicon="svg"]', {
      rel: 'icon',
      type: 'image/svg+xml',
      href: LOGO_PATHS.faviconSvg,
      'data-xsolia-favicon': 'svg'
    });

    upsertFavicon('link[data-xsolia-favicon="png"]', {
      rel: 'icon',
      type: 'image/png',
      href: LOGO_PATHS.faviconPng,
      'data-xsolia-favicon': 'png'
    });
  }

  document.addEventListener('DOMContentLoaded', function () {
    replaceNavLogoPlaceholder();
    ensureFavicons();
  });
})();
