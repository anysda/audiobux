<script lang="ts">
  // the blurred listener behind the player
  let { id, pc = false }: { id: string; pc?: boolean } = $props();
</script>

<div class="silh" aria-hidden="true">
  <svg viewBox="0 0 400 860" preserveAspectRatio={pc ? 'xMidYMax meet' : 'xMidYMid slice'}>
    <defs>
      <filter id="{id}a" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="15" /></filter>
      <filter id="{id}b" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="34" /></filter>
    </defs>
    <g class="sg" filter="url(#{id}a)">
      <ellipse cx="200" cy="292" rx="94" ry="120" />
      <path d="M158 380h84l10 74c76 16 128 46 160 104v90H-12v-90c32-58 84-88 160-104z" />
    </g>
    <ellipse class="sg" cx="200" cy="860" rx="120" ry="150" opacity=".8" filter="url(#{id}b)" />
  </svg>
</div>
