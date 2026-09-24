<script lang="ts">
  import { fit, type Fit } from './fit';

  let { tag = 'h2', class: cls = '', text, max, min, lines, height }: Fit & { tag?: string; class?: string } = $props();
  const words = $derived(text.split(/\s+/).filter(Boolean));
</script>

<svelte:element this={tag} class="disp {cls}" use:fit={{ text, max, min, lines, height }}
  >{#each words as w, i (i)}<span class="w"><span class="wi">{w}</span></span>{' '}{/each}</svelte:element
>
