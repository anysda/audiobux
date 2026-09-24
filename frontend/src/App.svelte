<script lang="ts">
  import { onMount } from 'svelte';
  import { lib } from './lib/state.svelte';
  import Phone from './Phone.svelte';

  onMount(() => lib.start());
</script>

<Phone />
