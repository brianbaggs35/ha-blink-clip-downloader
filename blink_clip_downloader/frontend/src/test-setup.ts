// jsdom doesn't implement matchMedia — PrimeVue's Select (and other overlay
// components) call it internally to react to viewport orientation, which
// throws in every test that mounts one without this polyfill.
if (!window.matchMedia) {
  window.matchMedia = (query: string): MediaQueryList =>
    ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    }) as MediaQueryList
}

// jsdom doesn't implement ResizeObserver — PrimeVue's Textarea (autoResize)
// and other size-reactive components use it internally, which throws in
// every test that mounts one without this polyfill.
if (!window.ResizeObserver) {
  window.ResizeObserver = class ResizeObserver {
    observe() {
      // Intentional no-op — this polyfill only needs to exist, not react.
    }
    unobserve() {
      // Intentional no-op — this polyfill only needs to exist, not react.
    }
    disconnect() {
      // Intentional no-op — this polyfill only needs to exist, not react.
    }
  }
}
