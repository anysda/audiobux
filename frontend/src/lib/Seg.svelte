<script lang="ts">
  let { options, value, label, onpick }: { options: string[]; value: number; label: string; onpick: (i: number) => void } =
    $props();
</script>

<div class="seg" role="group" aria-label={label} style:--n={options.length} style:--k={value}>
  <i></i>
  {#each options as o, i (i)}
    <button type="button" aria-pressed={i === value} onclick={() => onpick(i)}>{o}</button>
  {/each}
</div>
