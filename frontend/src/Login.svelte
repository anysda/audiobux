<script lang="ts">
  import { onMount } from 'svelte';
  import { api, ApiError } from './lib/api';
  import { EO, still } from './lib/motion';

  let username = $state('');
  let password = $state('');
  let error = $state('');
  let loading = $state(false);
  let card: HTMLElement;
  let user: HTMLInputElement;

  async function submit(e: SubmitEvent) {
    e.preventDefault();
    error = '';
    loading = true;
    try {
      await api.login(username, password);
      location.replace('/');
    } catch (err) {
      const s = err instanceof ApiError ? err.status : 0;
      error =
        s === 400
          ? 'Неверный логин или пароль'
          : s === 429
            ? 'Слишком много неудачных попыток, вход закрыт на 15 минут'
            : 'Не удалось войти';
      if (!still())
        card.animate(
          ['0', '-7px', '6px', '-3px', '0'].map((x) => ({ transform: `translateX(${x})` })),
          { duration: 340, easing: 'ease-out' },
        );
    } finally {
      loading = false;
    }
  }

  onMount(() => {
    user.focus();
    if (!still())
      card.animate([{ opacity: 0, transform: 'translateY(10px) scale(.98)' }, { opacity: 1, transform: 'none' }], {
        duration: 500,
        easing: EO,
        fill: 'backwards',
      });
  });
</script>

<main class="lg">
  <form class="lgc" bind:this={card} onsubmit={submit}>
    <h1 class="disp">audiobux</h1>
    <label class="lgf">
      <span class="mono">Логин</span>
      <input
        bind:this={user}
        bind:value={username}
        required
        disabled={loading}
        autocomplete="username"
        autocapitalize="none"
        spellcheck="false"
      />
    </label>
    <label class="lgf">
      <span class="mono">Пароль</span>
      <input bind:value={password} type="password" required disabled={loading} autocomplete="current-password" />
    </label>
    {#if error}<p class="lge mono" role="alert">{error}</p>{/if}
    <button class="padd lgo" type="submit" disabled={loading}>Войти</button>
  </form>
</main>
