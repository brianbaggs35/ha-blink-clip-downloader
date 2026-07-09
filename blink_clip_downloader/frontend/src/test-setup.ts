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
