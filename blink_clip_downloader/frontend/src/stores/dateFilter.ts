import { defineStore } from 'pinia'

/** Cross-tab bridge for the Status tab's activity-chart drill-down: clicking
 *  a day bar should switch to the Library tab and filter to that single day.
 *  `seq` increments on every request so a repeat click on the same date
 *  still re-triggers the watchers in App.vue (tab switch) and LibraryPage
 *  (query since/until) even though `date` itself hasn't changed. */
export const useDateFilterStore = defineStore('dateFilter', {
  state: () => ({
    date: null as string | null,
    seq: 0,
  }),
  actions: {
    requestDate(date: string) {
      this.date = date
      this.seq++
    },
  },
})
