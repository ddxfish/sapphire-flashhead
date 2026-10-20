import { mount } from './player.js';

let teardown = null;   // module is memoized across chat switches: one live mount

export function init(container) {
    teardown?.();
    const root = container.querySelector('#flashhead-sidebar') || container;
    teardown = mount(root);
}
