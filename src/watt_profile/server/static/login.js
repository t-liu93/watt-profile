"use strict";
(() => {
  const form = document.getElementById('login'), error = document.getElementById('error');
  form.addEventListener('submit', async event => {
    event.preventDefault(); const button = form.querySelector('button');
    if (button.disabled) return;
    button.disabled = true; error.hidden = true;
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(),22000);
    try {
      const response = await fetch('/login',{method:'POST',credentials:'same-origin',cache:'no-store',
        body:new URLSearchParams(new FormData(form)),signal:controller.signal});
      form.elements.password.value = '';
      if (response.ok && response.redirected) { window.location.assign('/'); return; }
      const result = await response.json(), code = result.error?.code;
      const messages = {
        invalid_credentials:'Administrator credentials are invalid. Retry with the Server account.',
        login_throttled:'Too many login attempts. Wait one minute, reload the login form and retry.',
        csrf_rejected:'Login form expired or verification failed. Reload the login form and retry.',
        temporarily_unavailable:'Server is temporarily unavailable. Reload the login form and retry.'
      };
      error.textContent = messages[code] || 'Login could not be completed. Reload the login form and retry.';
      error.hidden = false;
    } catch {
      error.textContent = 'Connection failed. Reload the login form and retry.'; error.hidden = false;
    } finally { clearTimeout(timer); form.elements.password.value = ''; button.disabled = false; }
  });
  window.addEventListener('pagehide',() => { form.elements.password.value = ''; });
})();
