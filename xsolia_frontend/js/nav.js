document.addEventListener('DOMContentLoaded', () => {
  // The navbar markup is now embedded directly into each page.
  // This script simply populates the user info and wires up the search form.
  const userId = localStorage.getItem('user_id');
  const name = localStorage.getItem('name') || 'Guest';
  const role = localStorage.getItem('role') || '';
  const subscription = localStorage.getItem('subscription') || '';

  const userInfoEl = document.getElementById('userInfo');
  const avatarEl = document.getElementById('userAvatar');
  const nameEl = document.getElementById('userNameLabel');
  const roleEl = document.getElementById('userRoleLabel');

  if (userInfoEl && avatarEl && nameEl && roleEl) {
    // Set avatar to first letter of name or '?'
    const initial = name.trim().charAt(0).toUpperCase() || '?';
    avatarEl.textContent = initial;
    nameEl.textContent = name;

    let roleLabel = 'Not signed in';
    if (userId && role) {
      if (role === 'creator') {
        roleLabel = 'Creator';
        if (subscription === 'creator_plus') {
          roleLabel += ' · Plus';
        }
      } else if (role === 'tester') {
        roleLabel = 'Tester';
      } else {
        roleLabel = role;
      }
    }
    roleEl.textContent = roleLabel;

    // Click handler to go to the appropriate dashboard based on role
    if (userId) {
      userInfoEl.style.cursor = 'pointer';
      userInfoEl.title =
        role === 'creator'
          ? 'Go to creator dashboard'
          : role === 'tester'
          ? 'Go to tester dashboard'
          : 'View account';

      userInfoEl.addEventListener('click', () => {
        const currentUserId = localStorage.getItem('user_id');
        const currentRole = localStorage.getItem('role');

        if (!currentUserId) {
          // Not logged in: direct to login page
          window.location.href = 'login.html';
          return;
        }

        if (currentRole === 'creator') {
          window.location.href = 'creator-dashboard.html';
        } else if (currentRole === 'tester') {
          window.location.href = 'tester-dashboard.html';
        } else {
          window.location.href = 'index.html';
        }
      });
    } else {
      userInfoEl.style.cursor = 'default';
    }
  }

  // Search form logic: redirect to explore.html?q=...
  const searchForm = document.getElementById('navSearchForm');
  const searchInput = document.getElementById('navSearchInput');
  if (searchForm && searchInput) {
    searchForm.addEventListener('submit', (e) => {
      e.preventDefault();
      const q = searchInput.value.trim();
      const base = 'explore.html';
      const url = q ? `${base}?q=${encodeURIComponent(q)}` : base;
      window.location.href = url;
    });
  }
});