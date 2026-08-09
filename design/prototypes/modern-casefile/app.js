(() => {
  const navItems = [...document.querySelectorAll('[data-screen-target]')];
  const screens = [...document.querySelectorAll('.screen')];
  const pageButtons = [...document.querySelectorAll('[data-page-target]')];
  const currentPage = document.querySelector('#current-page-number');
  const auditButton = document.querySelector('#record-decision');
  const decision = document.querySelector('#duplicate-decision');
  const auditStamp = document.querySelector('#audit-stamp');

  const showScreen = (screenId) => {
    screens.forEach((screen) => {
      const visible = screen.id === screenId;
      screen.hidden = !visible;
      screen.classList.toggle('is-visible', visible);
    });
    document.querySelectorAll('.nav-item[data-screen-target]').forEach((item) => {
      const active = item.dataset.screenTarget === screenId;
      item.classList.toggle('active', active);
      item.toggleAttribute('aria-current', active);
    });
    window.location.hash = screenId;
  };

  navItems.forEach((item) => {
    item.addEventListener('click', () => showScreen(item.dataset.screenTarget));
  });

  pageButtons.forEach((button) => {
    button.addEventListener('click', () => {
      const page = button.dataset.pageTarget;
      currentPage.textContent = page;
      pageButtons.forEach((item) => item.classList.toggle('active', item.dataset.pageTarget === page));
      document.querySelectorAll('.page-list li').forEach((item) => item.classList.remove('selected'));
      button.closest('li')?.classList.add('selected');
    });
  });

  auditButton?.addEventListener('click', () => {
    const value = decision.value;
    const labels = {
      pending: '模拟审计：未记录处置；仍待律师确认',
      exclude: '模拟审计：第 17 页仅在派生件提案中标记为排除；原件保持可见',
      keep: '模拟审计：第 17 页在派生件提案中标记为保留；原件保持可见',
    };
    auditStamp.textContent = labels[value];
  });

  if (window.location.hash === '#evidence') showScreen('evidence');
})();
