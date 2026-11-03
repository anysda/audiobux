import { expect, test } from 'vitest';
import { NOTCH, PACE, QUIET, STEP, step, turn, wheel } from './swipe';

const W = 400;

test('a long slow swipe turns one page the way the finger went', () => {
  expect(turn(3, 9, -W * 0.6, 900, W)).toBe(4);
  expect(turn(3, 9, W * 0.6, 900, W)).toBe(2);
});

test('a quick short flick turns one page', () => {
  expect(turn(3, 9, -40, 80, W)).toBe(4);
  expect(turn(3, 9, 40, 80, W)).toBe(2);
});

test('however far or fast, never more than one page', () => {
  expect(turn(3, 9, -W * 5, 50, W)).toBe(4);
  expect(turn(3, 9, W * 5, 50, W)).toBe(2);
});

test('short and slow, or a twitch, leaves the page', () => {
  expect(turn(3, 9, -W * 0.1, 900, W)).toBe(3);
  expect(turn(3, 9, -10, 5, W)).toBe(3);
  expect(turn(3, 9, 0, 0, W)).toBe(3);
});

test('the ends hold', () => {
  expect(turn(0, 9, W, 100, W)).toBe(0);
  expect(turn(9, 9, -W, 100, W)).toBe(9);
  expect(turn(0, 0, -W, 100, W)).toBe(0);
});

test('a mouse wheel turns one page per notch, either way', () => {
  const w = wheel();
  expect(w(NOTCH, 0)).toBe(1);
  expect(w(NOTCH, 500)).toBe(1);
  expect(w(-NOTCH, 1000)).toBe(-1);
});

test('notches spun quickly turn a page each, at no more than one per PACE', () => {
  const w = wheel();
  expect(w(NOTCH, 0)).toBe(1);
  expect(w(NOTCH, 40)).toBe(0);
  expect(w(NOTCH, PACE + 10)).toBe(1);
});

test('a trackpad push adds up small deltas and turns once, its fling included', () => {
  const w = wheel();
  let pages = 0;
  // the push: 3 px every 16 ms, then a fling that fades over a second
  for (let t = 0, d = 3; t < 200; t += 16) pages += w(d, t);
  for (let t = 200, d = 60; t < 1200; t += 16, d *= 0.93) pages += w(d, t);
  expect(pages).toBe(1);
  // the next push, after a pause, turns again
  for (let t = 1200 + QUIET + 20, n = 0; n < 10; t += 16, n++) pages += w(8, t);
  expect(pages).toBe(2);
});

test('a wheel delta below the step does not turn, and a change of way starts over', () => {
  const w = wheel();
  expect(w(STEP - 1, 0)).toBe(0);
  expect(w(-(STEP - 1), 10)).toBe(0);
  expect(w(-2, 20)).toBe(-1);
});

test('the keys of a reader turn a page, the rest do not', () => {
  expect(step('ArrowRight')).toBe(1);
  expect(step('PageDown')).toBe(1);
  expect(step(' ')).toBe(1);
  expect(step('ArrowLeft')).toBe(-1);
  expect(step('PageUp')).toBe(-1);
  expect(step(' ', true)).toBe(-1);
  expect(step('ArrowDown')).toBe(0);
  expect(step('a')).toBe(0);
});
