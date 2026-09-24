<script lang="ts">
  import { url, type Book } from './api';
  import { letter } from './text';

  let { book, el = $bindable() }: { book: Book; el?: HTMLElement } = $props();
  // the drawn cover stands in only when the book has no picture of its own
  let broken = $state(false);
</script>

<div class="cv" bind:this={el}>
  {#if book.has_cover && !broken}
    <img src={url.cover(book.id)} alt="" onerror={() => (broken = true)} />
  {:else}
    <i>{book.author}</i><b>{letter(book.title)}</b>
  {/if}
</div>
