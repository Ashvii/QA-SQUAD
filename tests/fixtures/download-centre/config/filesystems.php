<?php

return [
    'default' => 'local',

    'disks' => [
        'local' => [
            'driver' => 'local',
            'root' => storage_path('app/private'),
        ],

        'exports' => [
            'driver' => 'local',
            'root' => storage_path('app/exports'),
            'visibility' => 'private',
            'throw' => true,
        ],
    ],
];
