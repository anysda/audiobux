import { expect, test } from 'vitest';
import { turn } from './swipe';

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
