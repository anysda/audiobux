<script lang="ts">
  import { untrack } from 'svelte';
  import { url, type Book } from './api';
  import { letter } from './text';

  let { book, el = $bindable() }: { book: Book; el?: HTMLElement } = $props();
  // the drawn cover stands in only when the book has no picture of its own
  let broken = $state(false);
  // the picture is asked for again when it is another book's, has just appeared, or failed and the book moved on
  let v = $state(0);
  let seen = '';
  $effect.pre(() => {
    const k = `${book.id} ${book.has_cover}`,
      at = book.updated;
    untrack(() => {
      if (k === seen && !(broken && at !== v)) return;
      seen = k;
      v = at;
      broken = false;
    });
  });
</script>

<div class="cv" bind:this={el}>
  {#if book.has_cover && !broken && v}
    <img src={url.cover(book.id, v)} alt="" onerror={() => (broken = true)} />
  {:else}
    <i>{book.author}</i><b>{letter(book.title)}</b>
  {/if}
</div>
