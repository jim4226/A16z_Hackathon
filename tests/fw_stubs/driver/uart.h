#pragma once
// ESP-IDF UART driver subset
typedef int uart_port_t;
typedef enum { UART_MODE_UART = 0, UART_MODE_RS485_HALF_DUPLEX = 1 } uart_mode_t;
#define UART_NUM_1 1
#define UART_PIN_NO_CHANGE (-1)
int uart_set_pin(uart_port_t uart_num, int tx_io_num, int rx_io_num, int rts_io_num, int cts_io_num);
int uart_set_mode(uart_port_t uart_num, uart_mode_t mode);
