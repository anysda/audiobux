<script lang="ts">
  import { fit, words, type Fit } from './fit';

  let { tag = 'h2', class: cls = '', text, max, min, lines, height }: Fit & { tag?: string; class?: string } = $props();
  const ws = $derived(words(text));
</script>

<svelte:element this={tag} class="disp ttl {cls}" use:fit={{ text, max, min, lines, height }}
  >{#each ws as w, i (i)}{#each w as p, j (j)}{#if j}<wbr />{/if}<span class="w"><span class="wi">{p.replace(/_$/, '')}{#if p.endsWith('_')}<span class="us">_</span>{/if}</span></span>{/each}{' '}{/each}</svelte:element
>
