<script lang="ts">
  import { lib } from './state.svelte';
  import { fmt } from './text';

  let { id }: { id: string } = $props();
  let el = $state<HTMLElement>();
  let act = $state(false);
  let w = $state(0);

  const len = $derived(lib.length(lib.book(id)!) || 1);
  const p = $derived(Math.min(1, lib.at(id) / len));

  const to = (e: PointerEvent) => {
    const r = el!.getBoundingClientRect();
    lib.seek(id, ((e.clientX - r.left) / r.width) * len);
  };
</script>

<div
  class="scrub"
  class:act
  bind:this={el}
  bind:clientWidth={w}
  role="slider"
  aria-label="Позиция"
  aria-valuemin={0}
  aria-valuemax={Math.round(len)}
  aria-valuenow={Math.round(lib.at(id))}
  aria-valuetext={fmt(lib.at(id))}
  tabindex="0"
  onpointerdown={(e) => {
    el!.setPointerCapture(e.pointerId);
    act = true;
    to(e);
  }}
  onpointermove={(e) => act && to(e)}
  onpointerup={() => (act = false)}
  onpointercancel={() => (act = false)}
  onkeydown={(e) => {
    if (e.key === 'ArrowRight') lib.skip(id, 30);
    else if (e.key === 'ArrowLeft') lib.skip(id, -15);
    else return;
    e.preventDefault();
  }}
>
  <div class="track"><i style:transform="scaleX({p})"></i></div>
  <b class="knob" style:transform="translateX({p * w}px)"></b>
</div>
