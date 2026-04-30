document.addEventListener('DOMContentLoaded', () => {
  ensureNavbarMarkup();
  normalizeNavBrand();

  const userInfoEl = document.getElementById('userInfo');
  const avatarEl = document.getElementById('userAvatar');
  const nameEl = document.getElementById('userNameLabel');
  const roleEl = document.getElementById('userRoleLabel');

  if (userInfoEl && avatarEl && nameEl && roleEl) {
    hydrateUserSummary({ avatarEl, nameEl, roleEl });
    initAvatarMenu({ userInfoEl, avatarEl, nameEl, roleEl });
  }

  const searchForm = document.getElementById('navSearchForm');
  const searchInput = document.getElementById('navSearchInput');
  if (searchForm && searchInput) {
    searchForm.addEventListener('submit', (event) => {
      event.preventDefault();
      const query = searchInput.value.trim();
      const base = 'explore.html';
      const url = query ? `${base}?q=${encodeURIComponent(query)}` : base;
      window.location.href = url;
    });
  }
});

function ensureNavbarMarkup() {
  if (document.querySelector('.x-nav')) return;
  const host = document.getElementById('navbar');
  if (!host) return;

  host.innerHTML = `
    <header class="x-nav sticky top-0 z-50">
      <div class="container h-full flex items-center justify-between gap-4 py-3">
        <a href="index.html" class="flex items-center gap-2"></a>
        <div class="flex-1 flex items-center justify-end gap-4">
          <form id="navSearchForm" class="hidden md:flex items-center gap-2" style="max-width: 260px; width: 100%;">
            <input id="navSearchInput" type="search" class="x-input" placeholder="Search topics..." style="padding-right: 2.2rem;" />
            <button type="submit" style="margin-left: -2.1rem; font-size: 0.8rem; color: #9ca3af;">🔍</button>
          </form>
          <div id="userInfo" class="flex items-center gap-2" style="font-size: 0.8rem; color: #6b7280;">
            <div id="userAvatar" style="width: 28px; height: 28px; border-radius: 999px; background: linear-gradient(135deg, #7dd3fc, #a5b4fc); display: flex; align-items: center; justify-content: center; font-weight: 600; color: #fff; text-transform: uppercase;">?</div>
            <div class="flex flex-col">
              <span id="userNameLabel">Guest</span>
              <span id="userRoleLabel" style="font-size: 0.7rem; color: #9ca3af;">Not signed in</span>
            </div>
          </div>
        </div>
      </div>
    </header>
  `;
}

function normalizeNavBrand() {
  const navContainer = document.querySelector('.x-nav .container');
  if (!navContainer) return;

  const brandLink = navContainer.querySelector('a[href="index.html"]');
  if (!brandLink) return;

  brandLink.innerHTML = `
    <div>
      <picture class="site-logo-horizontal-wrap">
        <source srcset="assets/logo/logo-horizontal.svg" type="image/svg+xml" />
        <img
          class="site-logo-horizontal-img"
          src="assets/logo/logo-horizontal.png"
          alt="xSolia"
          width="140"
          height="30"
        />
      </picture>
      <div class="site-subtitle">Validation intelligence for builders</div>
    </div>
  `;
}

function getNavSession() {
  return {
    userId: localStorage.getItem('user_id') || '',
    name: localStorage.getItem('name') || 'Guest',
    role: localStorage.getItem('role') || '',
    subscription: localStorage.getItem('subscription') || '',
    username: localStorage.getItem('username') || '',
    avatarUrl: localStorage.getItem('avatar_url') || '',
  };
}

function getRoleLabel(session) {
  if (!session.userId || !session.role) return 'Not signed in';
  if (session.role === 'creator') {
    return session.subscription === 'creator_plus' ? 'Creator · Plus' : 'Creator';
  }
  if (session.role === 'tester') return 'Tester';
  return session.role;
}

function applyAvatarElement(target, session) {
  const initial = (session.name || 'Guest').trim().charAt(0).toUpperCase() || '?';
  target.textContent = '';
  target.innerHTML = '';

  if (session.avatarUrl) {
    const image = document.createElement('img');
    image.className = 'x-avatar-image';
    image.src = session.avatarUrl;
    image.alt = `${session.name || 'User'} avatar`;
    target.appendChild(image);
    return;
  }

  target.textContent = initial;
}

function hydrateUserSummary({ avatarEl, nameEl, roleEl }) {
  const session = getNavSession();
  applyAvatarElement(avatarEl, session);
  nameEl.textContent = session.name;
  roleEl.textContent = getRoleLabel(session);
}

function readFileAsDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ''));
    reader.onerror = () => reject(new Error('Failed to read image file'));
    reader.readAsDataURL(file);
  });
}

function pickAvatarFile() {
  return new Promise((resolve) => {
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = 'image/*';
    input.style.display = 'none';
    document.body.appendChild(input);

    input.addEventListener('change', () => {
      const file = input.files && input.files[0] ? input.files[0] : null;
      input.remove();
      resolve(file);
    });

    input.click();
  });
}

async function uploadAvatarImage(dataUrl) {
  if (!window.XsoliaApp || typeof window.XsoliaApp.apiFetch !== 'function') {
    throw new Error('Avatar update is unavailable on this page.');
  }

  const { response, data } = await window.XsoliaApp.apiFetch('/me/avatar', {
    method: 'PATCH',
    auth: true,
    body: { avatar_url: dataUrl },
  });

  if (!response.ok) {
    const message = window.XsoliaApp.formatApiError
      ? window.XsoliaApp.formatApiError(data, 'Failed to update avatar.')
      : 'Failed to update avatar.';
    throw new Error(message);
  }

  if (window.XsoliaApp.setSession) {
    window.XsoliaApp.setSession({ avatar_url: data.avatar_url });
  } else if (data.avatar_url) {
    localStorage.setItem('avatar_url', data.avatar_url);
  }
}

function initAvatarMenu({ userInfoEl, avatarEl, nameEl, roleEl }) {
  const menuEl = document.createElement('div');
  menuEl.className = 'x-user-menu-panel';
  menuEl.hidden = true;

  userInfoEl.classList.add('x-user-menu-anchor');
  userInfoEl.setAttribute('role', 'button');
  userInfoEl.setAttribute('tabindex', '0');
  userInfoEl.setAttribute('aria-haspopup', 'menu');
  userInfoEl.setAttribute('aria-expanded', 'false');
  userInfoEl.appendChild(menuEl);

  let isOpen = false;

  menuEl.addEventListener('click', (event) => {
    event.stopPropagation();
  });

  function closeMenu() {
    if (!isOpen) return;
    isOpen = false;
    menuEl.classList.remove('is-open');
    menuEl.hidden = true;
    userInfoEl.setAttribute('aria-expanded', 'false');
  }

  function refreshUserSummary() {
    const session = getNavSession();
    applyAvatarElement(avatarEl, session);
    nameEl.textContent = session.name;
    roleEl.textContent = getRoleLabel(session);
  }

  async function openCreatorPublicProfile() {
    const username = localStorage.getItem('username') || '';
    if (username) {
      window.location.href = `profile.html?u=${encodeURIComponent(username)}`;
      return;
    }

    const input = window.prompt('Choose a public username: 3-30 letters, numbers, or underscores.');
    if (!input) return;

    if (!window.XsoliaApp || typeof window.XsoliaApp.apiFetch !== 'function') {
      window.location.href = 'creator-dashboard.html';
      return;
    }

    try {
      const { response, data } = await window.XsoliaApp.apiFetch('/me/username', {
        method: 'PATCH',
        auth: true,
        body: { username: input },
      });
      if (!response.ok) {
        const message = window.XsoliaApp.formatApiError
          ? window.XsoliaApp.formatApiError(data, 'Failed to set username.')
          : 'Failed to set username.';
        window.alert(message);
        return;
      }

      if (window.XsoliaApp.setSession) {
        window.XsoliaApp.setSession({ username: data.username });
      } else if (data.username) {
        localStorage.setItem('username', data.username);
      }

      if (data.username) {
        window.location.href = `profile.html?u=${encodeURIComponent(data.username)}`;
      }
    } catch (_error) {
      window.alert('Failed to set username.');
    }
  }

  async function changeAvatar() {
    const file = await pickAvatarFile();
    if (!file) return;

    if (!file.type.startsWith('image/')) {
      window.alert('Please choose an image file.');
      return;
    }
    if (file.size > 2 * 1024 * 1024) {
      window.alert('Avatar image must be 2MB or smaller.');
      return;
    }

    try {
      const dataUrl = await readFileAsDataUrl(file);
      await uploadAvatarImage(dataUrl);
      refreshUserSummary();
    } catch (error) {
      window.alert(error.message || 'Failed to update avatar.');
    }
  }

  function appendMenuAction(containerEl, label, onClick, accent) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = `x-user-menu-item${accent ? ' is-accent' : ''}`;
    button.textContent = label;
    button.addEventListener('click', async () => {
      await onClick();
      closeMenu();
    });
    containerEl.appendChild(button);
  }

  function appendMenuLink(containerEl, label, href) {
    const link = document.createElement('a');
    link.className = 'x-user-menu-item';
    link.href = href;
    link.textContent = label;
    link.addEventListener('click', () => closeMenu());
    containerEl.appendChild(link);
  }

  function appendDivider(containerEl) {
    const divider = document.createElement('div');
    divider.className = 'x-user-menu-divider';
    containerEl.appendChild(divider);
  }

  function dashboardPath(session) {
    if (session.role === 'creator') return 'creator-dashboard.html';
    if (session.role === 'tester') return 'tester-dashboard.html';
    return 'index.html';
  }

  function rebuildMenu() {
    const session = getNavSession();
    const currentRoleLabel = getRoleLabel(session);

    refreshUserSummary();
    menuEl.innerHTML = '';

    const cardHeader = document.createElement('div');
    cardHeader.className = 'x-user-menu-head';

    const avatar = document.createElement('div');
    avatar.className = 'x-user-menu-head-avatar';
    applyAvatarElement(avatar, session);

    const identity = document.createElement('div');
    identity.className = 'x-user-menu-head-meta';
    const displayName = document.createElement('div');
    displayName.className = 'x-user-menu-head-name';
    displayName.textContent = session.name || 'Guest';
    const displayRole = document.createElement('div');
    displayRole.className = 'x-user-menu-head-role';
    displayRole.textContent = currentRoleLabel;

    identity.appendChild(displayName);
    identity.appendChild(displayRole);
    cardHeader.appendChild(avatar);
    cardHeader.appendChild(identity);
    menuEl.appendChild(cardHeader);

    appendDivider(menuEl);

    if (session.userId) {
      appendMenuLink(menuEl, 'Dashboard', dashboardPath(session));
      appendMenuLink(menuEl, 'Explore topics', 'explore.html');
      appendMenuLink(menuEl, 'Innovation pool', 'innovation-explore.html');
      appendMenuLink(menuEl, 'Leaderboard', 'leaderboard.html');
      appendMenuAction(menuEl, 'Change avatar', changeAvatar, false);

      if (session.role === 'creator') {
        appendMenuAction(menuEl, 'Public profile', openCreatorPublicProfile, false);
        appendMenuLink(menuEl, 'Create validation topic', 'create.html');
      }

      appendDivider(menuEl);
      appendMenuAction(
        menuEl,
        'Sign out',
        () => {
          if (window.XsoliaApp && typeof window.XsoliaApp.clearSession === 'function') {
            window.XsoliaApp.clearSession();
          } else {
            localStorage.removeItem('user_id');
            localStorage.removeItem('role');
            localStorage.removeItem('name');
            localStorage.removeItem('subscription');
            localStorage.removeItem('username');
            localStorage.removeItem('avatar_url');
            localStorage.removeItem('access_token');
          }
          window.location.href = 'index.html';
        },
        true
      );
      return;
    }

    appendMenuLink(menuEl, 'Log in', 'login.html');
    appendMenuLink(menuEl, 'Create creator account', 'register.html?role=creator');
    appendMenuLink(menuEl, 'Join as tester', 'register.html?role=tester');
    appendMenuLink(menuEl, 'Explore topics', 'explore.html');
    appendMenuLink(menuEl, 'Leaderboard', 'leaderboard.html');
  }

  function openMenu() {
    if (isOpen) return;
    rebuildMenu();
    isOpen = true;
    menuEl.hidden = false;
    requestAnimationFrame(() => menuEl.classList.add('is-open'));
    userInfoEl.setAttribute('aria-expanded', 'true');
  }

  function toggleMenu(event) {
    if (menuEl.contains(event.target)) return;
    event.preventDefault();
    event.stopPropagation();
    if (isOpen) {
      closeMenu();
      return;
    }
    openMenu();
  }

  userInfoEl.addEventListener('click', toggleMenu);
  userInfoEl.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') {
      toggleMenu(event);
    }
    if (event.key === 'Escape') {
      closeMenu();
    }
  });

  document.addEventListener('click', (event) => {
    if (!userInfoEl.contains(event.target)) {
      closeMenu();
    }
  });

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      closeMenu();
    }
  });

  window.addEventListener('resize', closeMenu);
}
