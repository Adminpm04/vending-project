cmake_minimum_required(VERSION 3.22.1)
project(vending_serial)

add_library(vending_serial SHARED serial_port.c)

find_library(log-lib log)
target_link_libraries(vending_serial ${log-lib})
