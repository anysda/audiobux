<script lang="ts">
  import { onMount } from 'svelte';
  import { MediaQuery } from 'svelte/reactivity';
  import { lib } from './lib/state.svelte';
  import Phone from './Phone.svelte';
  import Pc from './Pc.svelte';

  const wide = new MediaQuery('(min-width: 1000px)');
  onMount(() => lib.start());
</script>

{#if wide.current}<Pc />{:else}<Phone />{/if}
