<script lang="ts">
  import { lib } from './state.svelte';

  let shown = $state(false);
  $effect(() => {
    if (!lib.toast.n) return;
    shown = true;
    const t = setTimeout(() => (shown = false), 2400);
    return () => clearTimeout(t);
  });
</script>

<div class="toast mono" class:show={shown} role="status">{lib.toast.text}</div>
