<script lang="ts">
  import { onMount } from 'svelte';
  import { MediaQuery } from 'svelte/reactivity';
  import { lib } from './lib/state.svelte';
  import Login from './Login.svelte';
  import Phone from './Phone.svelte';
  import Pc from './Pc.svelte';

  const wide = new MediaQuery('(min-width: 1000px)');
  // the server gives this page before the sign-in and sends a signed-in browser on from it
  const signIn = location.pathname === '/login';
  onMount(() => {
    if (!signIn) lib.start();
  });
</script>

{#if signIn}<Login />{:else if wide.current}<Pc />{:else}<Phone />{/if}
