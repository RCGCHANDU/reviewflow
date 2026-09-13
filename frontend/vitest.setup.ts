import '@testing-library/jest-dom/vitest'

// jsdom implements neither of these, and both are used by the diff viewer and the
// event stream panel. Stubbing them keeps component tests honest rather than skipping
// the code paths that use them.
if (!('ResizeObserver' in globalThis)) {
  class ResizeObserverStub {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  Object.defineProperty(globalThis, 'ResizeObserver', { value: ResizeObserverStub })
}

if (!('scrollIntoView' in Element.prototype)) {
  Object.defineProperty(Element.prototype, 'scrollIntoView', { value: () => {} })
}
